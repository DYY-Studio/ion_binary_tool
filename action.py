from __future__ import (unicode_literals, division, absolute_import, print_function)

from functools import partial
import html
import os
import pickle
import re
import time
import traceback

from PyQt5.Qt import (Qt, QApplication, QIcon, QMenu, QProgressDialog, QToolButton)

from calibre.constants import numeric_version
from calibre.gui2 import (Dispatcher, error_dialog, open_url, question_dialog)
from calibre.gui2.actions import InterfaceAction
from calibre.ptempfile import PersistentTemporaryFile
from calibre.utils.logging import (ANSIStream, GUILog)

from calibre_plugins.kfx_input import KFXInput
from calibre_plugins.kfx_input.action_base import (ActionFromKFX, get_icons)
from calibre_plugins.kfx_input.config import (
    config_show_completion_popup, COMPLETION_POPUP_ALWAYS, COMPLETION_POPUP_ON_ERROR)
from calibre_plugins.kfx_input.jobs import (CalibreBook, html_to_text, value_unit)
from calibre_plugins.kfx_input.kfxlib import (KFXDRMError, set_logger, YJ_Book)


__license__ = "GPL v3"
__copyright__ = "2017-2025, John Howell <jhowell@acm.org>"

# resources contained in the plugin zip file
PLUGIN_ICON = "from_kfx_icon.png"


class UserCanceled(ValueError):
    pass


class FromKFXAction(InterfaceAction):
    name = ActionFromKFX.name
    type = ActionFromKFX.type
    version = KFXInput.version

    # Create our top-level menu/toolbar action (text, icon_path, tooltip, keyboard shortcut)
    action_spec = (ActionFromKFX.name, None, ActionFromKFX.description, None)
    popup_type = QToolButton.InstantPopup
    dont_add_to = frozenset(["menubar-device", "context-menu-device"])
    action_type = "current"

    def genesis(self):
        self.status_cache = {}
        self.icon = get_icons(PLUGIN_ICON, self.name) if numeric_version >= (5, 99, 3) else get_icons(PLUGIN_ICON)
        self.qaction.setIcon(self.icon)

        self.create_menu_actions()
        self.gui.keyboard.finalize()

        self.menu = QMenu(self.gui)
        self.set_default_menu()
        self.menu.aboutToShow.connect(self.set_customized_menu)
        self.menu.aboutToHide.connect(self.set_default_menu)
        self.qaction.setMenu(self.menu)

    def create_menu_actions(self):
        self.default_menu = m = QMenu(self.gui)

        # Actions that operate on the current selection

        self.convert_book_to_epub_action = self.create_menu_action(
            m, "FromKFXConvertBooksEPUB",
            "Convert selected to EPUB", "convert.png",
            description="Convert selected book(s) from KFX format to EPUB",
            triggered=partial(self.perform_conversion, "epub"))

        self.convert_book_to_pdf_action = self.create_menu_action(
            m, "FromKFXConvertBooksPDF",
            "Convert selected to PDF", "convert.png",
            description="Convert selected book(s) from KFX format to PDF",
            triggered=partial(self.perform_conversion, "pdf"))

        self.convert_book_to_cbz_action = self.create_menu_action(
            m, "FromKFXConvertBooksCBZ",
            "Convert selected to CBZ", "convert.png",
            description="Convert selected book(s) from KFX format to CBZ",
            triggered=partial(self.perform_conversion, "cbz"))

        m.addSeparator()

        # Actions that are not selection-based

        self.customize_action = self.create_menu_action(
            m, "FromKFXCustomize", "Customize plugin", "config.png",
            description="Configure the settings for this plugin",
            triggered=self.show_configuration)

        self.help_action = self.create_menu_action(
            m, "FromKFXHelp", "Help", "help.png",
            description="Display help for this plugin",
            triggered=self.show_help)

        # temporary actions and error messages (Not default actions)

        self.temp_menu = QMenu(self.gui)
        m = self.temp_menu

        self.input_format_error_action = self.add_menu_action(
            m, "(Selected book has no KFX format)", "dialog_error.png",
            "Book does not contain KFX, KFX-ZIP, or KPF format", enabled=False)

        self.drm_error_action = self.add_menu_action(
            m, "(Selected book has DRM)", "dialog_error.png",
            "Book is locked with DRM and cannot be converted", enabled=False)

        self.select_none_error_action = self.add_menu_action(
            m, "(No selected book)", "dialog_error.png",
            "No book is selected - Select a book with KFX format for conversion", enabled=False)

        self.select_multiple_error_action = self.add_menu_action(
            m, "(Multiple selected books)", "dialog_error.png",
            "Multiple books are selected - Select a single book with KFX format for conversion", enabled=False)

        self.select_multiple_action = self.add_menu_action(
            m, "(Multiple selected books)", "dialog_information.png",
            "Multiple books are selected - Those with KFX format will be converted", enabled=False)

        self.format_actions = {
            #"kfx": self.add_menu_action(m, "(Found KFX format)", "dialog_information.png", "", enabled=False),
            "kfx-zip": self.add_menu_action(m, "(Found KFX-ZIP format)", "dialog_error.png", "", enabled=False),
            "kpf": self.add_menu_action(m, "(Found KPF format)", "dialog_error.png", "", enabled=False)
            }

    def set_default_menu(self):
        # Copy actions from the default menu to the current menu
        self.menu.clear()

        for a in QMenu.actions(self.default_menu):
            self.menu.addAction(a)

    def set_customized_menu(self):
        # Build menu on the fly based on the number of books selected and actual formats
        m = self.menu
        m.clear()

        if self.gui.current_view() is self.gui.library_view:
            if self.gui.library_view.selectionModel().hasSelection():
                if len(self.gui.library_view.selectionModel().selectedRows()) == 1:
                    # If single book selected then check for KFX format
                    book_id = self.gui.library_view.get_selected_ids()[0]
                    input_format = self.kfx_format(book_id)

                    if input_format:
                        db = self.gui.current_db.new_api
                        file_name = db.format_abspath(book_id, input_format)    # original file used only for date check
                        modified_dt = os.path.getmtime(file_name) if os.path.isfile(file_name) else None

                        if file_name in self.status_cache and self.status_cache[file_name][0] == modified_dt:
                            is_drm_free, is_image_based = self.status_cache[file_name][1]
                        else:
                            try:
                                input_file_name = db.format(book_id, input_format, as_file=False, as_path=True, preserve_filename=True)
                                set_logger()
                                book = YJ_Book(input_file_name)
                                book.decode_book(retain_yj_locals=True)
                            except KFXDRMError:
                                is_drm_free = is_image_based = False
                            except Exception:
                                traceback.print_exc()
                                is_drm_free = True
                                is_image_based = False
                            else:
                                is_drm_free = True
                                is_image_based = book.is_image_based_fixed_layout

                            self.status_cache[file_name] = (modified_dt, (is_drm_free, is_image_based))     # cache for fast access

                        if input_format in self.format_actions:
                            m.addAction(self.format_actions[input_format])

                        if is_drm_free:
                            m.addAction(self.convert_book_to_epub_action)

                            if is_image_based:
                                m.addAction(self.convert_book_to_pdf_action)
                                m.addAction(self.convert_book_to_cbz_action)
                        else:
                            m.addAction(self.drm_error_action)
                    else:
                        m.addAction(self.input_format_error_action)
                else:
                    m.addAction(self.select_multiple_action)
                    m.addAction(self.convert_book_to_epub_action)
                    m.addAction(self.convert_book_to_pdf_action)
                    m.addAction(self.convert_book_to_cbz_action)
            else:
                m.addAction(self.select_none_error_action)

            m.addSeparator()

        m.addAction(self.customize_action)
        m.addAction(self.help_action)

    def book_formats(self, book_id):
        return set([x.lower().strip() for x in self.gui.current_db.new_api.formats(book_id)])

    def kfx_format(self, book_id):
        formats = self.book_formats(book_id)
        for input_format in ["kfx", "kfx-zip", "kpf"]:
            # choose highest priority format that this book has (if any)
            if input_format in formats:
                return input_format
        return None

    def perform_conversion(self, to_fmt):
        selected_ids = self.gui.library_view.get_selected_ids()
        if not selected_ids:
            error_dialog(self.gui, "No books selected", "Select one or more books to enable conversion", show=True)
            return

        db = self.gui.current_db.new_api
        calibre_books = []
        convert_count = overwrite_count = 0

        for book_id in selected_ids:
            mi = db.get_proxy_metadata(book_id)
            input_format = self.kfx_format(book_id)

            if input_format:
                input_file = PersistentTemporaryFile(".%s" % input_format)
                with input_file:
                    db.copy_format_to(book_id, input_format, input_file)

                input_filename = input_file.name
                input_file.close()

                if to_fmt in self.book_formats(book_id):
                    overwrite_count += 1

                output_file = PersistentTemporaryFile(".%s" % to_fmt)
                output_filename = output_file.name
                output_file.close()
                convert_count += 1
            else:
                input_filename = output_filename = None

            calibre_books.append(CalibreBook(
                id=book_id, authors=mi.authors, title=mi.title, last_modified=mi.last_modified,
                input_filename=input_filename, output_filename=output_filename, to_fmt=to_fmt))

        if not convert_count:
            error_dialog(
                self.gui, "No selected book contains KFX, KFX-ZIP, or KPF format",
                "Select one or more books with KFX format for conversion", show=True)
            return

        if overwrite_count:
            if not question_dialog(
                    self.gui, "%s format already present in %s" % (to_fmt.upper(), value_unit(overwrite_count, "book")),
                    "<p>If you proceed, that format will be overwritten.<p>Do you want to proceed?"):
                return

        self.start_conversion_job(calibre_books)

    def start_conversion_job(self, calibre_books):
        self.gui.job_manager.run_job(Dispatcher(self.conversion_complete), "arbitrary_n", args=[
                "calibre_plugins.kfx_input.jobs", "convert_process", (pickle.dumps(calibre_books),)],
                description="Convert from KFX")

        self.gui.status_bar.show_message("%s conversion started" % self.name, 3000)

    def conversion_complete(self, job):
        # Called upon completion of a background job

        if job.failed:
            self.gui.job_exception(job, dialog_title="%s conversion job failed!" % self.name)
            return

        self.gui.status_bar.show_message("%s conversion job completed" % self.name, 3000)

        if not hasattr(job, "html_details"):
            job.html_details = job.details  # Details from ParallelJob are actually html.

        calibre_books = job.result
        log = job.html_details

        message = []
        details = []
        converted = failed = 0

        if calibre_books:
            try:
                db = self.gui.current_db
                updated_ids = []

                for cbook in calibre_books:
                    details.append("%s: %s" % (cbook.title, cbook.message))

                    if cbook.success and cbook.id is not None and db.new_api.has_id(cbook.id):
                        # add format
                        db.new_api.add_format(cbook.id, cbook.to_fmt, cbook.output_filename, replace=True, run_hooks=False)
                        os.remove(cbook.output_filename)

                        updated_ids.append(cbook.id)
                        converted += 1
                    else:
                        failed += 1

                # Update the gui view to reflect changes to the database
                if updated_ids:
                    self.gui.library_view.model().refresh_ids(updated_ids)
                    current = self.gui.library_view.currentIndex()
                    self.gui.library_view.model().current_changed(current, current)
                    self.gui.tags_view.recount()

            except Exception as e:
                traceback.print_exc()
                error_dialog(self.gui, "Unhandled exception updating formats", repr(e), det_msg=traceback.format_exc(), show=True)

            if converted:
                message.append(value_unit(converted, "converted book"))

            if failed:
                message.append(value_unit(failed, "failed conversion"))
        else:
            message.append("No results")
            failed = 0

        message.append("(click 'View log' for details)")

        show_popup = config_show_completion_popup()
        if show_popup == COMPLETION_POPUP_ALWAYS or (show_popup == COMPLETION_POPUP_ON_ERROR and failed):
            self.gui.proceed_question(
                self.proceed_do_nothing, None, log, self.name + " Log",
                self.name + ": Job complete", ". ".join(message),
                det_msg=html_to_text("\n\n".join(details)), show_copy_button=True,
                show_det=True, show_ok=True, cancel_callback=self.proceed_do_nothing,
                icon=self.icon)

    def proceed_do_nothing(self, _):
        pass

    def add_menu_action(self, menu, text, image=None, tooltip=None, triggered=None, enabled=True, submenu=None):
        # Minimal version without keyboard shortcuts, etc.

        ac = menu.addAction(text)

        if tooltip:
            ac.setToolTip(tooltip)
            ac.setStatusTip(tooltip)    # This is the only one actually used
            ac.setWhatsThis(tooltip)

        if triggered:
            ac.triggered.connect(triggered)

        if image:
            ac.setIcon(QIcon(I(image)))

        ac.setEnabled(enabled)

        if submenu:
            ac.setMenu(submenu)

        return ac

    def show_configuration(self):
        self.interface_action_base_plugin.do_user_config(self.gui)

    def show_help(self):
        open_url("https://www.mobileread.com/forums/showthread.php?t=291290")

    def update_progress(self, pct_complete):
        self.progress_dialog.update(pct_complete)
        if self.progress_dialog.wasCanceled():
            raise UserCanceled


class GUILog2(GUILog):
    def __init__(self):
        GUILog.__init__(self)
        self.outputs.append(ANSIStream())    # enable console output


def clean_log(lg, wrap=True):
    # QT ignores text that looks like HTML tags such as <error>
    # fix HTML errors that prevent display of some messages. Leave spans since they are added by logger
    z = []
    for x in re.split("(<[^<]*)", lg):
        if x.startswith("<") and (not (x.startswith("<span") or x.startswith("</span"))):
            #print("strip: %s" % x)
            z.append("&lt;")
            z.append(x[1:])
        else:
            #print("keep: %s" % x)
            z.append(x)

    if wrap:
        return "<html><pre style=\"font-family: monospace\">" + ("".join(z)) + "</pre></html>"

    return "".join(z)


def add_text_to_html(text):
    return "<br/>" + html.escape(text).replace("\n", "<br/>")


class ProgressDialog(QProgressDialog):
    def __init__(self, parent, name, message):
        QProgressDialog.__init__(self, parent)
        self.setWindowTitle(name)
        self.setLabelText(message)
        self.setWindowFlags(self.windowFlags() & (~Qt.WindowContextHelpButtonHint) & (~Qt.WindowCloseButtonHint))
        self.setMinimumDuration(0)
        self.setModal(True)
        self.setRange(0, 100)
        self.setAutoReset(False)    # do not close at 100%
        self.show()
        self.setValue(0)

        for _ in range(200):
            time.sleep(0.001)
            QApplication.processEvents()    # hack to get "in progress" dialog to display

    def update(self, pct_complete):
        self.setValue(pct_complete)
        QApplication.processEvents()
