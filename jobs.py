import html
import os
import pickle
import platform
import sys
import traceback

from calibre.constants import (get_version, numeric_version)
from calibre.utils.logging import (HTMLStream, INFO, Log)

from calibre_plugins.kfx_input import (get_symbol_catalog_filename)
from calibre_plugins.kfx_input.action_base import ActionFromKFX
from calibre_plugins.kfx_input.config import (config_desaturate_notebooks, config_split_landscape_comic_images)
from calibre_plugins.kfx_input.kfxlib import (file_write_binary, KFXDRMError, set_logger, YJ_Book)

__license__ = "GPL v3"
__copyright__ = "2025, John Howell <jhowell@acm.org>"


LOG_SEP = '\n\n<table bgcolor="darkBlue" width="80%" height="3" align="center"><tr><td></td></tr></table>\n'


def convert_process(pickled_calibre_books, notification=lambda x, y: x):
    log = set_logger(XJobLog(XLog()))
    calibre_books = pickle.loads(pickled_calibre_books)
    results = []
    report_version(log, ActionFromKFX)
    log.info("")

    for book_num, cbook in enumerate(calibre_books):
        try:
            msg = Message()

            if book_num > 0:
                log.separate()

            log.info("Processing %s" % cbook.title)

            if cbook.input_filename:
                book = YJ_Book(cbook.input_filename, symbol_catalog_filename=get_symbol_catalog_filename())
                book.decode_book(retain_yj_locals=True)
                cbook.success = False
                progress_message = "Converting %s to %s" % (cbook.title, cbook.to_fmt.upper())

                def update_progress(pct_complete):
                    notification((book_num + (pct_complete / 100.0)) / len(calibre_books), progress_message)

                if cbook.to_fmt == "epub":
                    try:
                        from calibre.ebooks.conversion.config import load_defaults
                        epub2_desired = load_defaults("epub_output").get("epub_version", "2") == "2"
                    except Exception:
                        log.info("Failed to read default EPUB Output preferences")
                        epub2_desired = True

                    epub_data = book.convert_to_epub(
                        epub2_desired=epub2_desired,
                        desaturate_notebooks=config_desaturate_notebooks(),
                        progress_fn=update_progress)
                    file_write_binary(cbook.output_filename, epub_data)
                    log.info(msg("Converted book to EPUB"))
                    cbook.success = True

                elif cbook.to_fmt == "cbz":
                    if book.is_image_based_fixed_layout:
                        cbz_data = book.convert_to_cbz(progress_fn=update_progress)
                        if cbz_data:
                            file_write_binary(cbook.output_filename, cbz_data)
                            log.info(msg("Converted book images to CBZ"))
                            cbook.success = True
                        else:
                            log.error(msg("Failed to create CBZ format"))
                    else:
                        log.error(msg("Book format does not support CBZ conversion - must be image based fixed-layout"))

                elif cbook.to_fmt == "pdf":
                    if book.is_image_based_fixed_layout:
                        pdf_data = book.convert_to_pdf(
                            split_landscape_comic_images=config_split_landscape_comic_images(), progress_fn=update_progress)
                        if pdf_data:
                            file_write_binary(cbook.output_filename, pdf_data)
                            log.info(msg("Extracted PDF content" if book.has_pdf_resource else "Converted book images to PDF"))
                            cbook.success = True
                        else:
                            log.error(msg("Failed to create PDF format"))
                    else:
                        log.error(msg("Book format does not support PDF conversion - must be image based fixed-layout"))

                else:
                    log.error(msg("Unknown conversion output format %s" % cbook.to_fmt))

                if book.has_pdf_resource and cbook.to_fmt != "pdf":
                    log.warning("This book contains PDF content. Convert to PDF to extract it.")

                os.remove(cbook.input_filename)

            else:
                log.error(msg("Book does not contain KFX format"))
                cbook.success = False

        except KFXDRMError:
            log.error(msg("This book is locked by DRM and cannot be converted"))
            cbook.success = False

        except Exception as e:
            traceback.print_exc()
            log.error(msg("Unhandled exception: %s" % repr(e)))
            cbook.success = False

        cbook.message = msg.get()
        results.append(cbook)

    set_logger()
    return results


class CalibreBook(object):
    def __init__(self, id, authors='', title='', input_filename=None, output_filename=None, to_fmt=None, last_modified=None):
        self.id = id
        self.authors = authors
        self.title = title
        self.last_modified = last_modified
        self.input_filename = input_filename
        self.output_filename = output_filename
        self.to_fmt = to_fmt
        self.success = False
        self.message = ""


def report_version(log, plugin):
    try:
        platform_info = platform.platform()
    except Exception:
        platform_info = sys.platform     # handle failure to retrieve platform seen on linux

    log.info("Software versions: %s %s, calibre %s, %s" % (plugin.name, ".".join([str(v) for v in plugin.version]),
             get_version(), platform_info))
    log.info("KFX Input plugin help is available at https://www.mobileread.com/forums/showthread.php?t=291290")


class Message(object):
    def __init__(self):
        self.msg = ""

    def __call__(self, msg):
        self.msg = msg
        return msg

    def get(self):
        return self.msg


class XHTMLStream(HTMLStream):
    # Logging stream that produces a cleaner job details display than HTMLStream does for ParallelJob

    def prints(self, level, *args, **kwargs):
        if numeric_version < (5, 7, 0):
            kwargs['file'] = self.stream

        if level == INFO:
            self._prints(*args, **kwargs)   # Don't add <span> tags for normal INFO logs
        else:
            self.stream.write(self.color[level])
            new_args = list(args)
            new_args.append(self.normal)    # Merge </span> with preceding text for cleaner output
            self._prints(*new_args, **kwargs)


class XLog(Log):
    def __init__(self):
        Log.__init__(self, level=Log.DEBUG)
        self.outputs = [XHTMLStream()]   # output to sys.stdout with html formatting


class XJobLog(object):
    def __init__(self, logger):
        self.logger = logger

    def debug(self, msg):
        self.logger.debug(html_escape(msg))

    def info(self, msg):
        self.logger.info(html_escape(msg))

    def warn(self, msg):
        self.logger.warn("WARNING: " + html_escape(msg))

    def warning(self, msg):
        self.warn(msg)

    def error(self, msg):
        self.logger.error("ERROR: " + html_escape(msg))

    def separate(self):
        self.logger.error(LOG_SEP)


class HTMLTextExtractor(html.parser.HTMLParser):
    def __init__(self):
        html.parser.HTMLParser.__init__(self)
        self.result = []

    def handle_data(self, d):
        self.result.append(d)

    def handle_charref(self, number):
        codepoint = int(number[1:], 16) if number[0] in (u'x', u'X') else int(number)
        self.result.append(chr(codepoint))

    def handle_entityref(self, name):
        if name in html.entities.name2codepoint:
            codepoint = html.entities.name2codepoint[name]
            self.result.append(chr(codepoint))
        else:
            self.result.append('&%s;' % name)   # cannot decode, leave entity unchanged

    def get_text(self):
        return u''.join(self.result)


def html_to_text(htm):
    s = HTMLTextExtractor()
    s.feed(htm)
    return s.get_text()


def html_escape(msg):
    return html.escape(msg, quote=False)


def value_unit(value, unit):
    if value == 1:
        return '1 %s' % unit            # singular: 1 book

    if unit[-1] == 's':
        units = '%ses' % unit           # plural: 2 bosses

    elif unit[-1] == 'y':
        units = '%sies' % unit[:-1]     # plural: 3 libraries

    else:
        units = '%ss' % unit            # plural: 4 books

    return '%s %s' % ('{:,}'.format(value), units)  # add commas for large numbers
