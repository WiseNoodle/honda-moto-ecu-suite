import os
import wx
from eculib.honda import *

from .base import HondaECUAppPanel

downloads = os.path.join(os.path.expanduser("~"), "Downloads")

class HondaECUEEPROMPanel(HondaECUAppPanel):
    def Build(self):
        self.wildcard = "EEPROM dump (*.bin)|*.bin"
        self.byts = None
        self.mainp = wx.Panel(self)

        self.formatbox = wx.RadioBox(
            self.mainp,
            label="Fill byte",
            choices=["0x00", "0xFF"]
        )

        self.wfilel = wx.StaticText(self.mainp, label="File")

        self.readfpicker = wx.FilePickerCtrl(
            self.mainp,
            wildcard="EEPROM dump (*.bin)|*.bin",
            path=os.path.join(downloads, "ecu_dump.bin"),
            style=wx.FLP_SAVE | self.HONDAECU_FILE_STYLE
        )

        self.writefpicker = wx.FilePickerCtrl(
            self.mainp,
            wildcard=self.wildcard,
            style=wx.FLP_OPEN | wx.FLP_FILE_MUST_EXIST | self.HONDAECU_FILE_STYLE
        )

        self.progressboxp = wx.Panel(self.mainp)
        self.progressbox = wx.BoxSizer(wx.VERTICAL)

        self.lastpulse = time.time()

        self.progress = wx.Gauge(
            self.progressboxp,
            style=wx.GA_HORIZONTAL | wx.GA_SMOOTH
        )
        self.progress.SetRange(100)

        self.progressboxp.Hide()

        self.progress_text = wx.StaticText(
            self.progressboxp,
            size=(32, -1),
            style=wx.ALIGN_CENTRE_HORIZONTAL
        )

        self.progressbox.Add(
            self.progress,
            1,
            flag=wx.EXPAND
        )

        self.progressbox.Add(
            self.progress_text,
            0,
            flag=wx.EXPAND | wx.TOP,
            border=10
        )

        self.progressboxp.SetSizer(self.progressbox)

        self.gobutton = wx.Button(self.mainp, label="Read")
        self.gobutton.Disable()

        self.fpickerbox = wx.BoxSizer(wx.HORIZONTAL)
        self.fpickerbox.AddSpacer(5)
        self.fpickerbox.Add(self.readfpicker, 1)
        self.fpickerbox.Add(self.writefpicker, 1)

        self.modebox = wx.RadioBox(
            self.mainp,
            label="Mode",
            choices=["Read", "Write", "Format"]
        )

        self.eeprompsizer = wx.GridBagSizer()

        self.eeprompsizer.Add(
            self.wfilel,
            pos=(0, 0),
            flag=wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
            border=10
        )

        self.eeprompsizer.Add(
            self.fpickerbox,
            pos=(0, 1),
            span=(1, 5),
            flag=wx.EXPAND | wx.RIGHT | wx.BOTTOM,
            border=10
        )

        self.eeprompsizer.Add(
            self.progressboxp,
            pos=(3, 0),
            span=(1, 6),
            flag=wx.BOTTOM | wx.LEFT | wx.RIGHT | wx.EXPAND | wx.TOP,
            border=20
        )

        self.eeprompsizer.Add(
            self.modebox,
            pos=(4, 0),
            span=(1, 2),
            flag=wx.ALIGN_LEFT | wx.ALIGN_BOTTOM | wx.LEFT | wx.TOP,
            border=30
        )

        self.eeprompsizer.Add(
            self.gobutton,
            pos=(5, 5),
            flag=wx.ALIGN_RIGHT | wx.ALIGN_BOTTOM | wx.RIGHT,
            border=10
        )

        self.eeprompsizer.AddGrowableRow(2, 1)
        self.eeprompsizer.AddGrowableCol(5, 1)

        # Put the controls into the EEPROM sizer.
        self.mainp.SetSizer(self.eeprompsizer)

        # Main panel layout
        self.mainsizer = wx.BoxSizer(wx.VERTICAL)
        self.mainsizer.Add(self.mainp, 1, wx.EXPAND)

        self.SetSizer(self.mainsizer)

        # Make the file picker column expand
        self.eeprompsizer.AddGrowableCol(1, 1)

        self.readfpicker.Hide()
        self.formatbox.Hide()

        self.Layout()
        self.mainp.Layout()

        self.OnModeChange(None)

    def OnModeChange(self, event):
        mode = self.modebox.GetSelection()

        if mode == 0:
            self.readfpicker.Show()
            self.writefpicker.Hide()
            self.formatbox.Hide()

        elif mode == 1:
            self.readfpicker.Hide()
            self.writefpicker.Show()
            self.formatbox.Hide()

        elif mode == 2:
            self.readfpicker.Hide()
            self.writefpicker.Hide()
            self.formatbox.Show()

        self.Layout()