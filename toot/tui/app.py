import logging
import urwid

from concurrent.futures import ThreadPoolExecutor

from toot import api, __version__

from .compose import StatusComposer
from .constants import PALETTE
from .entities import Status
from .overlays import ExceptionStackTrace, GotoMenu, Help, StatusSource
from .timeline import Timeline
from .utils import show_media

logger = logging.getLogger(__name__)


class Header(urwid.WidgetWrap):
    def __init__(self, app, user):
        self.app = app
        self.user = user

        self.text = urwid.Text("")
        self.cols = urwid.Columns([
            ("pack", urwid.Text(('header_bold', 'toot'))),
            ("pack", urwid.Text(('header', f' | {user.username}@{app.instance}'))),
            ("pack", self.text),
        ])

        widget = urwid.AttrMap(self.cols, 'header')
        widget = urwid.Padding(widget)
        self._wrapped_widget = widget

    def clear_text(self, text):
        self.text.set_text("")

    def set_text(self, text):
        self.text.set_text(" | " + text)


class Footer(urwid.Pile):
    def __init__(self):
        self.status = urwid.Text("")
        self.message = urwid.Text("")

        return super().__init__([
            urwid.AttrMap(self.status, "footer_status"),
            urwid.AttrMap(self.message, "footer_message"),
        ])

    def set_status(self, text):
        self.status.set_text(text)

    def clear_status(self, text):
        self.status.set_text("")

    def set_message(self, text):
        self.message.set_text(text)

    def set_error_message(self, text):
        self.message.set_text(("footer_message_error", text))

    def clear_message(self):
        self.message.set_text("")


class TUI(urwid.Frame):
    """Main TUI frame."""

    @classmethod
    def create(cls, app, user):
        """Factory method, sets up TUI and an event loop."""

        tui = cls(app, user)
        loop = urwid.MainLoop(
            tui,
            palette=PALETTE,
            event_loop=urwid.AsyncioEventLoop(),
            unhandled_input=tui.unhandled_input,
        )
        tui.loop = loop

        return tui

    def __init__(self, app, user):
        self.app = app
        self.user = user

        self.loop = None  # set in `create`
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.timeline_generator = api.home_timeline_generator(app, user, limit=40)

        # Show intro screen while toots are being loaded
        self.body = self.build_intro()
        self.header = Header(app, user)
        self.footer = Footer()
        self.footer.set_status("Loading...")

        self.timeline = None
        self.overlay = None
        self.exception = None

        super().__init__(self.body, header=self.header, footer=self.footer)

    def run(self):
        self.loop.set_alarm_in(0, lambda *args:
            self.async_load_timeline(is_initial=True, timeline_name="home"))
        self.loop.run()
        self.executor.shutdown(wait=False)

    def build_intro(self):
        font = urwid.font.Thin6x6Font()

        # NB: Padding with width="clip" will convert the fixed BigText widget
        # to a flow widget so it can be used in a Pile.

        big_text = "Toot {}".format(__version__)
        big_text = urwid.BigText(("intro_bigtext", big_text), font)
        big_text = urwid.Padding(big_text, align="center", width="clip")

        intro = urwid.Pile([
            big_text,
            urwid.Divider(),
            urwid.Text([
                "Maintained by ",
                ("intro_smalltext", "@ihabunek"),
                " and contributors"
            ], align="center"),
            urwid.Divider(),
            urwid.Text(("intro_smalltext", "Loading toots..."), align="center"),
        ])

        return urwid.Filler(intro)

    def run_in_thread(self, fn, args=[], kwargs={}, done_callback=None, error_callback=None):
        """Runs `fn(*args, **kwargs)` asynchronously in a separate thread.

        On completion calls `done_callback` if `fn` exited cleanly, or
        `error_callback` if an exception was caught. Callback methods are
        invoked in the main thread, not the thread in which `fn` is executed.
        """

        def _default_error_callback(ex):
            self.exception = ex
            self.footer.set_error_message("An exeption occured, press E to view")

        _error_callback = error_callback or _default_error_callback

        def _done(future):
            try:
                result = future.result()
                if done_callback:
                    # Use alarm to invoke callback in main thread
                    self.loop.set_alarm_in(0, lambda *args: done_callback(result))
            except Exception as ex:
                exception = ex
                logger.exception(exception)
                self.loop.set_alarm_in(0, lambda *args: _error_callback(exception))

        future = self.executor.submit(fn, *args, **kwargs)
        future.add_done_callback(_done)
        return future

    def connect_default_timeline_signals(self, timeline):
        def _compose(*args):
            self.show_compose()

        def _reply(timeline, status):
            logger.info("reply")
            self.show_compose(status)

        def _source(timeline, status):
            self.show_status_source(status)

        def _media(timeline, status):
            self.show_media(status)

        def _menu(timeline, status):
            self.show_context_menu(status)

        urwid.connect_signal(timeline, "focus", self.refresh_footer)
        urwid.connect_signal(timeline, "reblog", self.async_toggle_reblog)
        urwid.connect_signal(timeline, "favourite", self.async_toggle_favourite)
        urwid.connect_signal(timeline, "source", _source)
        urwid.connect_signal(timeline, "compose", _compose)
        urwid.connect_signal(timeline, "reply", _reply)
        urwid.connect_signal(timeline, "media", _media)
        urwid.connect_signal(timeline, "menu", _menu)

    def build_timeline(self, name, statuses):
        def _close(*args):
            raise urwid.ExitMainLoop()

        def _next(*args):
            self.async_load_timeline(is_initial=False)

        def _thread(timeline, status):
            self.show_thread(status)

        timeline = Timeline(name, statuses)

        self.connect_default_timeline_signals(timeline)
        urwid.connect_signal(timeline, "next", _next)
        urwid.connect_signal(timeline, "close", _close)
        urwid.connect_signal(timeline, "thread", _thread)

        return timeline

    def show_thread(self, status):
        def _close(*args):
            """When thread is closed, go back to the main timeline."""
            self.body = self.timeline
            self.body.refresh_status_details()
            self.refresh_footer(self.timeline)

        # This is pretty fast, so it's probably ok to block while context is
        # loaded, can be made async later if needed
        context = api.context(self.app, self.user, status.id)
        ancestors = [Status(s, self.app.instance) for s in context["ancestors"]]
        descendants = [Status(s, self.app.instance) for s in context["descendants"]]
        statuses = ancestors + [status] + descendants
        focus = len(ancestors)

        timeline = Timeline("thread", statuses, focus, is_thread=True)

        self.connect_default_timeline_signals(timeline)
        urwid.connect_signal(timeline, "close", _close)

        self.body = timeline
        self.refresh_footer(timeline)

    def async_load_timeline(self, is_initial, timeline_name=None):
        """Asynchronously load a list of statuses."""

        def _load_statuses():
            self.footer.set_message("Loading statuses...")
            try:
                data = next(self.timeline_generator)
            except StopIteration:
                return []
            finally:
                self.footer.clear_message()

            return [Status(s, self.app.instance) for s in data]

        def _done_initial(statuses):
            """Process initial batch of statuses, construct a Timeline."""
            self.timeline = self.build_timeline(timeline_name, statuses)
            self.timeline.refresh_status_details()  # Draw first status
            self.refresh_footer(self.timeline)
            self.body = self.timeline

        def _done_next(statuses):
            """Process sequential batch of statuses, adds statuses to the
            existing timeline."""
            self.timeline.append_statuses(statuses)

        return self.run_in_thread(_load_statuses,
            done_callback=_done_initial if is_initial else _done_next)

    def refresh_footer(self, timeline):
        """Show status details in footer."""
        status, index, count = timeline.get_focused_status_with_counts()
        self.footer.set_status([
            ("footer_status_bold", "[{}] ".format(timeline.name)),
            status.id, " - status ", str(index + 1), " of ", str(count),
        ])

    def show_status_source(self, status):
        self.open_overlay(
            widget=StatusSource(status),
            title="Status source",
        )

    def show_exception(self, exception):
        self.open_overlay(
            widget=ExceptionStackTrace(exception),
            title="Unhandled Exception",
        )

    def show_compose(self, in_reply_to=None):
        def _close(*args):
            self.close_overlay()

        def _post(timeline, *args):
            self.post_status(*args)

        composer = StatusComposer(in_reply_to)
        urwid.connect_signal(composer, "close", _close)
        urwid.connect_signal(composer, "post", _post)
        self.open_overlay(composer, title="Compose status")

    def show_goto_menu(self):
        menu = GotoMenu()
        urwid.connect_signal(menu, "home_timeline",
            lambda x: self.goto_home_timeline())
        urwid.connect_signal(menu, "public_timeline",
            lambda x, local: self.goto_public_timeline(local))
        urwid.connect_signal(menu, "hashtag_timeline",
            lambda x, tag, local: self.goto_tag_timeline(tag, local=local))

        self.open_overlay(menu, title="Go to", options=dict(
            align="center", width=("relative", 60),
            valign="middle", height=9,
        ))

    def show_help(self):
        self.open_overlay(Help(), title="Help")

    def goto_home_timeline(self):
        self.timeline_generator = api.home_timeline_generator(
            self.app, self.user, limit=40)
        promise = self.async_load_timeline(is_initial=True, timeline_name="home")
        promise.add_done_callback(lambda *args: self.close_overlay())

    def goto_public_timeline(self, local):
        self.timeline_generator = api.public_timeline_generator(
            self.app.instance, local=local, limit=40)
        promise = self.async_load_timeline(is_initial=True, timeline_name="public")
        promise.add_done_callback(lambda *args: self.close_overlay())

    def goto_tag_timeline(self, tag, local):
        self.timeline_generator = api.tag_timeline_generator(
            self.app.instance, tag, local=local, limit=40)
        promise = self.async_load_timeline(is_initial=True, timeline_name="#{}".format(tag))
        promise.add_done_callback(lambda *args: self.close_overlay())

    def show_media(self, status):
        urls = [m["url"] for m in status.data["media_attachments"]]
        if urls:
            show_media(urls)

    def show_context_menu(self, status):
        # TODO: show context menu
        pass

    def post_status(self, content, warning, visibility, in_reply_to_id):
        data = api.post_status(self.app, self.user, content,
            spoiler_text=warning,
            visibility=visibility,
            in_reply_to_id=in_reply_to_id)
        status = Status(data, self.app.instance)

        # TODO: instead of this, fetch new items from the timeline?
        self.timeline.prepend_status(status)
        self.timeline.focus_status(status)

        self.footer.set_message("Status posted {} \\o/".format(status.id))
        self.close_overlay()

    def async_toggle_favourite(self, timeline, status):
        def _favourite():
            logger.info("Favouriting {}".format(status))
            api.favourite(self.app, self.user, status.id)

        def _unfavourite():
            logger.info("Unfavouriting {}".format(status))
            api.unfavourite(self.app, self.user, status.id)

        def _done(loop):
            # Create a new Status with flipped favourited flag
            new_data = status.data
            new_data["favourited"] = not status.favourited
            timeline.update_status(Status(new_data, status.instance))

        self.run_in_thread(
            _unfavourite if status.favourited else _favourite,
            done_callback=_done
        )

    def async_toggle_reblog(self, timeline, status):
        def _reblog():
            logger.info("Reblogging {}".format(status))
            api.reblog(self.app, self.user, status.id)

        def _unreblog():
            logger.info("Unreblogging {}".format(status))
            api.unreblog(self.app, self.user, status.id)

        def _done(loop):
            # Create a new Status with flipped reblogged flag
            new_data = status.data
            new_data["reblogged"] = not status.reblogged
            timeline.update_status(Status(new_data, status.instance))

        self.run_in_thread(
            _unreblog if status.reblogged else _reblog,
            done_callback=_done
        )

    # --- Overlay handling -----------------------------------------------------

    default_overlay_options = dict(
        align="center", width=("relative", 80),
        valign="middle", height=("relative", 80),
    )

    def open_overlay(self, widget, options={}, title=""):
        top_widget = urwid.LineBox(widget, title=title)
        bottom_widget = self.body

        _options = self.default_overlay_options.copy()
        _options.update(options)

        self.overlay = urwid.Overlay(
            top_widget,
            bottom_widget,
            **_options
        )
        self.body = self.overlay

    def close_overlay(self):
        self.body = self.overlay.bottom_w
        self.overlay = None

    # --- Keys -----------------------------------------------------------------

    def unhandled_input(self, key):
        # TODO: this should not be in unhandled input
        if key in ('e', 'E'):
            if self.exception:
                self.show_exception(self.exception)

        elif key in ('g', 'G'):
            if not self.overlay:
                self.show_goto_menu()

        elif key in ('h', 'H'):
            if not self.overlay:
                self.show_help()

        elif key == 'esc':
            if self.overlay:
                self.close_overlay()

        elif key in ('q', 'Q'):
            if self.overlay:
                self.close_overlay()
            else:
                raise urwid.ExitMainLoop()
