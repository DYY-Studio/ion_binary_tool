from __future__ import (unicode_literals, division, absolute_import, print_function)

from PyQt5.Qt import (QGroupBox, QVBoxLayout, QWidget, QCheckBox, QComboBox, QHBoxLayout, QLabel)

from calibre.utils.config import JSONConfig


__license__ = "GPL v3"
__copyright__ = "2017-2025, John Howell <jhowell@acm.org>"

# Individual Setting names
ShowCompletionPopup = "ShowCompletionPopup"
COMPLETION_POPUP_ON_ERROR = 0
COMPLETION_POPUP_ALWAYS = 1
COMPLETION_POPUP_NEVER = 2

DesaturateNotebooks = "DesaturateNotebooks"
SplitLandscapeComicImages = "SplitLandscapeComicImages"

# Set location where all preferences for this plugin will be stored
plugin_config = JSONConfig("plugins/KFX Input")

# Default values
plugin_config.defaults[DesaturateNotebooks] = False
plugin_config.defaults[ShowCompletionPopup] = COMPLETION_POPUP_ON_ERROR
plugin_config.defaults[SplitLandscapeComicImages] = False


class ConfigWidget(QWidget):
    def __init__(self):
        QWidget.__init__(self)

        layout = QVBoxLayout(self)
        layout.addWidget(self.options_group_box())
        layout.addStretch()
        self.setLayout(layout)

    def options_group_box(self):
        group_box = QGroupBox("Options:", self)
        layout = QVBoxLayout()
        group_box.setLayout(layout)

        completion_popup_layout = QHBoxLayout()
        completion_popup_layout.addWidget(QLabel("Show 'From KFX' completion popup:"))
        self.ShowCompletionPopup = QComboBox()
        self.ShowCompletionPopup.setToolTip(
            "Select whether or not a notification popup will be shown upon completion of a 'From KFX' conversion job.")
        self.ShowCompletionPopup.addItem("Only on conversion error")
        self.ShowCompletionPopup.addItem("Always")
        self.ShowCompletionPopup.addItem("Never")
        completion_popup_layout.addWidget(self.ShowCompletionPopup)
        self.ShowCompletionPopup.setCurrentIndex(plugin_config[ShowCompletionPopup])
        layout.addLayout(completion_popup_layout)

        self.SplitLandscapeComicImages = QCheckBox("Split landscape images when converting comics to PDF")
        self.SplitLandscapeComicImages.setToolTip(
            "Causes landscape orientation images in comics to be split into separate left and right side images\n"
            "when converting to PDF format. This is intended to break page spreads into individual page\n"
            "images. This option only applies to conversion done using the plugin CLI or From KFX GUI, not\n"
            "conversion using calibre's Convert Books feature.")
        layout.addWidget(self.SplitLandscapeComicImages)
        self.SplitLandscapeComicImages.setChecked(plugin_config[SplitLandscapeComicImages])

        self.DesaturateNotebooks = QCheckBox("Desaturate colors when converting Scribe Colorsoft notebooks")
        self.DesaturateNotebooks.setToolTip(
            "Mute the colors in Scribe notebooks to better match the files produced by Amazon when exporting them as PDF.")
        layout.addWidget(self.DesaturateNotebooks)
        self.DesaturateNotebooks.setChecked(plugin_config[DesaturateNotebooks])

        layout.addStretch()
        return group_box

    def save_settings(self):
        # Called by calibre when the configuration dialog has been accepted
        plugin_config[ShowCompletionPopup] = self.ShowCompletionPopup.currentIndex()
        plugin_config[SplitLandscapeComicImages] = self.SplitLandscapeComicImages.isChecked()
        plugin_config[DesaturateNotebooks] = self.DesaturateNotebooks.isChecked()


def config_show_completion_popup():
    return plugin_config[ShowCompletionPopup]


def config_split_landscape_comic_images():
    return plugin_config[SplitLandscapeComicImages]


def config_desaturate_notebooks():
    return plugin_config[DesaturateNotebooks]
