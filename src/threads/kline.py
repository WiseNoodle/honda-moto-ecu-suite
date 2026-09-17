import os
import struct
import time
from threading import Thread

import numpy as np
from pydispatch import dispatcher
import pylibftdi
from pylibftdi import Device, FtdiError
from usb.core import USBError
import wx

from eculib import KlineAdapter
from eculib.honda import *
from m32r import M32RDiagnostic


# Fix bug in pylibftdi where __del__ checks self._opened
# before it is created.
def _safe_ftdi_del(self):
    if getattr(self, "_opened", False):
        try:
            self.close()
        except Exception:
            pass


Device.__del__ = _safe_ftdi_del


class KlineWorker(Thread):

    def __init__(self, parent):
        self.parent = parent

        self.__clear_data()

        dispatcher.connect(
            self.DeviceHandler,
            signal="FTDIDevice",
            sender=dispatcher.Any
        )

        dispatcher.connect(
            self.ErrorPanelHandler,
            signal="ErrorPanel",
            sender=dispatcher.Any
        )

        dispatcher.connect(
            self.DatalogPanelHandler,
            signal="DatalogPanel",
            sender=dispatcher.Any
        )

        dispatcher.connect(
            self.ReadPanelHandler,
            signal="ReadPanel",
            sender=dispatcher.Any
        )

        dispatcher.connect(
            self.WritePanelHandler,
            signal="WritePanel",
            sender=dispatcher.Any
        )

        dispatcher.connect(
            self.HRCSettingsPanelHandler,
            signal="HRCSettingsPanel",
            sender=dispatcher.Any
        )

        dispatcher.connect(
            self.SettingsHandler,
            signal="settings",
            sender=dispatcher.Any
        )

        dispatcher.connect(
            self.PasswordHandler,
            signal="sendpassword",
            sender=dispatcher.Any
        )

        dispatcher.connect(
            self.EEPROMHandler,
            signal="eeprom",
            sender=dispatcher.Any
        )

        Thread.__init__(self)

    def __cleanup(self):
        if self.ecu:
            try:
                if getattr(self.ecu, "dev", None) is not None:
                    self.ecu.dev.close()
            except Exception:
                pass

            try:
                del self.ecu
            except Exception:
                pass

        self.__clear_data()

    def __clear_data(self):
        self.ecu = None
        self.m32r = None
        self.password = None

        self.ready = False

        self.sendpassword = False
        self.sendwriteinit = False
        self.sendrecoverinint = False

        self.hrcmode = None
        self.securemode = False

        self.senderase = False

        self.readinfo = None
        self.writeinfo = None
        self.eeprominfo = None

        self.state = ECUSTATE.UNKNOWN

        self.reset_state()

    def reset_state(self):
        self.ecmid = bytearray()
        self.errorcodes = {}

        self.update_errors = False
        self.clear_codes = False

        self.flashcount = -1
        self.dtccount = -1

        self.update_tables = False
        self.tables = None
        self.tables_probed = False

        wx.CallAfter(
            dispatcher.send,
            signal="KlineWorker",
            sender=self,
            info="ecmid",
            value=bytes(self.ecmid)
        )

        wx.CallAfter(
            dispatcher.send,
            signal="KlineWorker",
            sender=self,
            info="flashcount",
            value=self.flashcount
        )

        wx.CallAfter(
            dispatcher.send,
            signal="KlineWorker",
            sender=self,
            info="state",
            value=self.state
        )

    # ---------------------------------------------------------
    # SETTINGS
    # ---------------------------------------------------------

    def apply_settings(self, config):
        """
        Apply configuration settings to the connected ECU device.
        """

        if self.ecu is None:
            return

        dev = getattr(self.ecu, "dev", None)

        if dev is None:
            return

        try:
            defaults = config["DEFAULT"]

            dev.timeout = float(defaults["timeout"])
            dev.retries = int(defaults["retries"])

            if defaults["klinemethod"] == "poll_modem_status":
                dev.kline = dev.kline_poll_modem_status
            else:
                dev.kline = dev.kline_loopback_ping

            dev.kline_timeout = float(
                defaults["kline_timeout"]
            )

            dev.kline_wait = float(
                defaults["kline_wait"]
            )

            dev.kline_testbytes = int(
                defaults["kline_testbytes"]
            )

        except (
            KeyError,
            ValueError,
            TypeError,
            AttributeError
        ):
            pass

    def SettingsHandler(self, config):
        self.apply_settings(config)

    # ---------------------------------------------------------
    # OTHER HANDLERS
    # ---------------------------------------------------------

    def HRCSettingsPanelHandler(self, mode, data):
        self.hrcmode = (mode, data)

    def WritePanelHandler(self, data, offset):
        self.writeinfo = [data, offset, None]

    def EEPROMHandler(self, cmd, data):
        self.eeprominfo = [cmd, data, None]

    def PasswordHandler(self, passwd):
        if self.state != ECUSTATE.SECURE:
            self.sendpassword = True
            self.password = passwd

    def ReadPanelHandler(self, data, offset):
        print("[KLINE] ReadPanelHandler RECEIVED:", repr(data), "offset:", hex(offset))
        self.readinfo = [data, offset, None]
        print("[KLINE] readinfo SET:", repr(self.readinfo))

    def DatalogPanelHandler(self, action):
        print("[KLINE] DatalogPanelHandler:", repr(action))

        if action == "data.on":
            print("[KLINE] LIVE DATA ENABLED")
            self.update_tables = True

        elif action == "data.off":
            print("[KLINE] LIVE DATA DISABLED")
            self.update_tables = False
            self.tables_probed = False

    def ErrorPanelHandler(self, action):
        if action == "dtc.clear":
            self.clear_codes = True

        elif action == "dtc.on":
            self.update_errors = True

        elif action == "dtc.off":
            self.update_errors = False

    # ---------------------------------------------------------
    # DEVICE HANDLER
    # ---------------------------------------------------------

    def DeviceHandler(self, action, device, config):
        print("[KLINE] DeviceHandler:",
            "action=", repr(action),
            "device=", repr(device))

        print("[KLINE] config =", repr(config))

        if action == "interrupt":
            raise Exception()

        elif action == "deactivate":
            print("[KLINE] deactivate")

            if self.ecu:
                self.__cleanup()

        elif action == "activate":
            print("[KLINE] ACTIVATE START")

            self.__clear_data()

            adapter = None

            # The control panel gives us the actual FTDI device object.
            # Use it directly first.
            try:
                print("[KLINE] Creating KlineAdapter(config)...")

                adapter = KlineAdapter(config)

                print(
                    "[KLINE] KlineAdapter(config) SUCCESS:",
                    repr(adapter)
                )

            except Exception as e:
                print(
                    "[KLINE] KlineAdapter(config) FAILED:",
                    repr(e)
                )

            # If that doesn't work, use the actual FTDI serial number.
            if adapter is None:
                device_id = None

                if hasattr(config, "serial_number"):
                    device_id = config.serial_number
                elif hasattr(config, "serial"):
                    device_id = config.serial
                elif hasattr(config, "device_id"):
                    device_id = config.device_id

                print(
                    "[KLINE] config device_id =",
                    repr(device_id)
                )

                if device_id:
                    try:
                        print(
                            "[KLINE] Creating KlineAdapter(device_id)..."
                        )

                        adapter = KlineAdapter(str(device_id))

                        print(
                            "[KLINE] KlineAdapter(device_id) SUCCESS:",
                            repr(adapter)
                        )

                    except Exception as e:
                        print(
                            "[KLINE] KlineAdapter(device_id) FAILED:",
                            repr(e)
                        )

            if adapter is None:
                print(
                    "[KLINE] FAILED: could not create KlineAdapter"
                )

                wx.CallAfter(
                    dispatcher.send,
                    signal="KlineWorker",
                    sender=self,
                    info="error",
                    value="Could not create K-line adapter"
                )

                return

            try:
                print("[KLINE] Creating HondaECU...")

                self.ecu = HondaECU(adapter)

                try:
                    print("[KLINE] Initial wakeup")

                    self.ecu.init()
                    self.ecu.ping()

                    detected = self.ecu.detect_ecu_state()

                    print(
                        "[KLINE] Initial ECU state:",
                        repr(detected)
                    )

                except Exception as e:
                    print(
                        "[KLINE] Wakeup failed:",
                        repr(e)
                    )

                print(
                    "[KLINE] HondaECU SUCCESS:",
                    repr(self.ecu)
                )

                try:
                    self.m32r = M32RDiagnostic(self.ecu)

                    print(
                        "[KLINE] M32RDiagnostic SUCCESS:",
                        repr(self.m32r)
                    )

                except Exception as e:
                    self.m32r = None

                    print(
                        "[KLINE] M32RDiagnostic FAILED:",
                        repr(e)
                    )

                print("[KLINE] Applying settings...")

                self.apply_settings(self.parent.config)

                print("[KLINE] Settings applied")

                self.ready = True

                print("[KLINE] READY = True")

            except Exception as e:
                print(
                    "[KLINE] ECU SETUP FAILED:",
                    repr(e)
                )

                self.ecu = None
                self.m32r = None
                self.ready = False

                wx.CallAfter(
                    dispatcher.send,
                    signal="KlineWorker",
                    sender=self,
                    info="error",
                    value=str(e)
                )

    # ---------------------------------------------------------
    # EEPROM
    # ---------------------------------------------------------

    def read_eeprom(self):

        ret = "good"

        binfile = self.eeprominfo[1]

        rom = bytearray()
        offset = 0

        while (
            self.eeprominfo is not None
            and offset <= 0xff
        ):

            if not self.parent.run:
                return "interrupted"

            status, data = (
                self.ecu.pgmfi_read_eeprom_word(
                    offset
                )
            )

            if status:

                rom += bytearray(data)
                offset += 1

                wx.CallAfter(
                    dispatcher.send,
                    signal="KlineWorker",
                    sender=self,
                    info="read_eeprom.progress",
                    value=(
                        offset / 256 * 100.0,
                        None
                    )
                )

            else:
                ret = "bad"
                break

        if self.ecu.dev.kline():

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="read_eeprom.progress",
                value=(
                    offset / 256 * 100.0,
                    None
                )
            )

        else:
            return "interrupted"

        if ret == "good":

            end = 512

            if rom[:256] == rom[256:]:
                end = 256

            with open(binfile, "wb") as eeprom:
                eeprom.write(rom[:end])
                eeprom.flush()

        return ret

    # ---------------------------------------------------------
    # FLASH READ
    # ---------------------------------------------------------

    def read_flash(self):
        print("[KLINE] READ: Waiting for ignition OFF...")

        if self.ecu.dev.kline():
            print("[KLINE] Please turn the key OFF.")
            while self.ecu.dev.kline():
                time.sleep(0.1)

        print("[KLINE] Ignition OFF detected.")
        print("[KLINE] Please turn the key ON.")

        for i in range(20):
            kstate = self.ecu.dev.kline()
            print("[KLINE] kline() while OFF =", repr(kstate))
            if kstate:
                break
            time.sleep(0.5)

        if not kstate:
            print("[KLINE] ERROR: Ignition ON was not detected.")
            return False

        print("[KLINE] Ignition ON detected.")
        time.sleep(0.5)

        print("[KLINE] READ: Reinitializing Honda ECU...")
        try:
            self.ecu.init()
            print("[KLINE] READ: Honda ECU reinitialization complete.")
        except Exception as e:
            print("[KLINE] READ: Honda ECU reinitialization FAILED:", repr(e))
            return False


        print("[KLINE] READ: Entering diagnostic/security sequence...")

        try:
            print("[KLINE] READ: Security access step 1...")
            info = self.ecu.send_command(
                [0x27],
                [0xe0, 0x48, 0x65, 0x6c, 0x6c, 0x6f, 0x48, 0x6f],
                retries=1
            )
            print("[KLINE] READ: Security step 1 response:", repr(info))

            if info is None:
                print("[KLINE] READ: Security step 1 FAILED - stopping read.")
                return False

            print("[KLINE] READ: Security access step 2...")
            info = self.ecu.send_command(
                [0x27],
                [0xe0, 0x77, 0x41, 0x72, 0x65, 0x59, 0x6f, 0x75],
                retries=1
            )
            print("[KLINE] READ: Security step 2 response:", repr(info))

            if info is None:
                print("[KLINE] READ: Security step 2 FAILED - stopping read.")
                return False

        except Exception as e:
            print("[KLINE] READ INIT ERROR:", repr(e))
            return False


        readsize = 8
        location = offset = self.readinfo[1]
        binfile = self.readinfo[0]
        status = "bad"

        with open(binfile, "wb") as fbin:
            t = time.time()
            size = location
            rate = 0

            while self.readinfo is not None:

                if not self.parent.run:
                    return "interrupted"

                try:
                    address = (
                        [int(location / 65536)]
                        + list(struct.pack("<H", location % 65536))
                        + [readsize]
                    )

                    print("[KLINE] FLASH READ LOCATION:", hex(location))
                    print("[KLINE] FLASH READ SIZE:", readsize)
                    print("[KLINE] FLASH READ ADDRESS:", address)

                    info = self.ecu.send_command(
                        [0x82, 0x82, 0x00],
                        address
                    )

                    print("[KLINE] FLASH READ RESPONSE:", repr(info))

                    if info is not None:
                        data = info[2]
                        status = True
                    else:
                        data = None
                        status = False

                except Exception as e:
                    print("[KLINE] FLASH READ ERROR:", repr(e))
                    data = None
                    status = False

                if status:
                    fbin.write(data)
                    fbin.flush()

                    location += readsize

                    n = time.time()

                    v = (
                        -1,
                        "%.02fKB @ %s" %
                        (
                            (location - offset) / 1024.0,
                            "%.02fB/s" % rate
                            if rate > 0
                            else "---"
                        )
                    )

                    wx.CallAfter(
                        dispatcher.send,
                        signal="KlineWorker",
                        sender=self,
                        info="read.progress",
                        value=v
                    )

                    if n - t > 1:
                        rate = (
                            (location - size)
                            / (n - t)
                        )

                        t = n
                        size = location

                else:
                    readsize -= 1

                    if readsize < 1:
                        break

            if self.ecu.dev.kline():
                v = (
                    -1,
                    "%.02fKB @ %s" %
                    (
                        (location - offset) / 1024.0,
                        "%.02fB/s" % rate
                        if rate > 0
                        else "---"
                    )
                )

                wx.CallAfter(
                    dispatcher.send,
                    signal="KlineWorker",
                    sender=self,
                    info="read.progress",
                    value=v
                )

            else:
                return "interrupted"

        with open(binfile, "rb") as fbin:
            nbyts = os.path.getsize(binfile)

            if nbyts > 0:
                byts = bytearray(
                    fbin.read(nbyts)
                )

                print("[KLINE] READ FILE SIZE:", nbyts)
                print("[KLINE] FIRST BYTES:", repr(byts[:16]))

                _, status, _ = do_validation(
                    byts,
                    nbyts
                )

                print("[KLINE] VALIDATION RESULT:", repr(status))

            else:
                print("[KLINE] READ FILE IS EMPTY")

            return status
            
        
    # ---------------------------------------------------------
    # EEPROM WRITE
    # ---------------------------------------------------------

    def write_eeprom(self, byts):

        maxi = len(byts) / 2
        i = 0

        while (
            self.eeprominfo is not None
            and i < maxi
        ):

            status, data = (
                self.ecu.pgmfi_write_eeprom_word(
                    i,
                    byts[i:(i + 2)]
                )
            )

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="write_eeprom.progress",
                value=(
                    i / maxi * 100,
                    None
                )
            )

            if status:

                i += 1

            else:

                wx.CallAfter(
                    dispatcher.send,
                    signal="KlineWorker",
                    sender=self,
                    info="write_eeprom.progress",
                    value=(
                        0,
                        "interrupted"
                    )
                )

                return 1

        return 0

    # ---------------------------------------------------------
    # FLASH WRITE
    # ---------------------------------------------------------

    def write_flash(self, byts, offset=0):

        ossize = len(byts)

        offseti = int(offset / 16)

        writesize = 128
        z = int(writesize / 16)

        maxi = int(ossize / writesize)

        i = 0

        w = i * writesize

        t = time.time()

        rate = 0
        size = 0

        while (
            self.writeinfo is not None
            and i < maxi
        ):

            bytstart = [
                s for s in struct.pack(
                    ">H",
                    offseti + (z * i)
                )
            ]

            if i + 1 == maxi:

                bytend = [
                    s for s in struct.pack(
                        ">H",
                        0
                    )
                ]

            else:

                bytend = [
                    s for s in struct.pack(
                        ">H",
                        offseti + (z * (i + 1))
                    )
                ]

            d = list(
                byts[
                    ((i + 0) * writesize):
                    ((i + 1) * writesize)
                ]
            )

            x = bytstart + d + bytend

            c1 = checksum8bit(x)
            c2 = checksum8bitHonda(x)

            x = [
                0x01,
                0x06
            ] + x + [
                c1,
                c2
            ]

            info = self.ecu.send_command(
                [0x7e],
                x
            )

            if info is not None:

                ilen = ord(info[1])

                if ilen != 5:

                    if ilen == 7:

                        if struct.unpack(
                            ">H",
                            info[2][2:4]
                        )[0] != (
                            offseti + (z * (i + 1))
                        ):

                            wx.CallAfter(
                                dispatcher.send,
                                signal="KlineWorker",
                                sender=self,
                                info="write.progress",
                                value=(
                                    0,
                                    "unexpected return"
                                )
                            )

                            return 1

                        else:

                            self.ecu.dev.stats[
                                "unneeded_retry"
                            ] += 1

                            dispatcher.send(
                                signal="ecu.stats",
                                sender=self,
                                data=self.ecu.dev.stats
                            )

            else:

                if i == 0:

                    if writesize == 128:

                        writesize = 64
                        z = int(
                            writesize / 16
                        )

                        maxi = int(
                            ossize / writesize
                        )

                        continue

                    else:

                        wx.CallAfter(
                            dispatcher.send,
                            signal="KlineWorker",
                            sender=self,
                            info="write.progress",
                            value=(
                                0,
                                "failed"
                            )
                        )

                        return 2

                else:

                    wx.CallAfter(
                        dispatcher.send,
                        signal="KlineWorker",
                        sender=self,
                        info="write.progress",
                        value=(
                            0,
                            "max retries"
                        )
                    )

                    return 3

            n = time.time()

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="write.progress",
                value=(
                    i / maxi * 100,
                    "%.02fKB of %.02fKB @ %s" %
                    (
                        w / 1024.0,
                        ossize / 1024.0,
                        "%.02fB/s" % rate
                        if rate > 0
                        else "---"
                    )
                )
            )

            if n - t > 1:

                rate = (
                    (w - size)
                    / (n - t)
                )

                t = n
                size = w

            i += 1

            if i % 2 == 0:

                if writesize == 64:

                    self.ecu.send_command(
                        [0x7e],
                        [0x01, 0x07]
                    )

                    time.sleep(.200)

            w = i * writesize

        r = rate if rate > 0 else "---"

        v = (
            i / maxi * 100,
            "%.02fKB of %.02fKB @ %s" %
            (
                (w - offset) / 1024.0,
                ossize / 1024.0,
                "%.02fB/s" % r
            )
        )

        wx.CallAfter(
            dispatcher.send,
            signal="KlineWorker",
            sender=self,
            info="progress",
            value=v
        )

        return 0

    # ---------------------------------------------------------
    # WRITE INITIALIZATION
    # ---------------------------------------------------------

    def do_init_write(self, recover=False):

        self.do_update_state()

        if recover:

            self.ecu.do_init_recover()

            self.ecu.send_command(
                [0x72],
                [0x00, 0xf1]
            )

            time.sleep(1)

            self.ecu.send_command(
                [0x27],
                [0x00, 0x01, 0x00]
            )

        else:

            self.ecu.do_init_write()

        self.do_update_state()

        time.sleep(.100)

    # ---------------------------------------------------------
    # ERASE
    # ---------------------------------------------------------

    def do_erase(self, wait=11):

        self.do_update_state()

        ret = 1

        wx.CallAfter(
            dispatcher.send,
            signal="KlineWorker",
            sender=self,
            info="erase",
            value=None
        )

        self.ecu.get_write_status()

        w = wait

        for i in range(wait):

            w = wait - i

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="write.progress",
                value=(
                    w / wait * 100,
                    "waiting for %d seconds" % w
                )
            )

            time.sleep(1)

        wx.CallAfter(
            dispatcher.send,
            signal="KlineWorker",
            sender=self,
            info="write.progress",
            value=(
                0,
                "waiting for %d seconds" % w
            )
        )

        if self.ecu.do_erase():

            time.sleep(2)

            e = 0

            while True:

                wx.CallAfter(
                    dispatcher.send,
                    signal="KlineWorker",
                    sender=self,
                    info="write.progress",
                    value=(
                        np.clip(
                            e / 35 * 100,
                            0,
                            100
                        ),
                        "erasing ecu"
                    )
                )

                time.sleep(.1)

                e += 1

                info = self.ecu.send_command(
                    [0x7e],
                    [0x01, 0x05]
                )

                if info:

                    if info[2][1] == 0x00:

                        self.ecu.get_write_status()

                        ret = 0
                        break

                    elif info[2][1] == 0xfa:

                        wx.CallAfter(
                            dispatcher.send,
                            signal="KlineWorker",
                            sender=self,
                            info="write.progress",
                            value=(
                                0,
                                "erase block error"
                            )
                        )

                        ret = 2
                        break

                else:
                    break

        else:

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="write.progress",
                value=(
                    0,
                    "erase failed"
                )
            )

        self.do_update_state()

        return ret

    # ---------------------------------------------------------
    # WRITE
    # ---------------------------------------------------------

    def do_write(self):

        self.do_update_state()

        ret = 1

        wx.CallAfter(
            dispatcher.send,
            signal="KlineWorker",
            sender=self,
            info="write",
            value=None
        )

        if self.write_flash(
            self.writeinfo[0],
            offset=self.writeinfo[1]
        ) == 0:

            self.writeinfo[2] = (
                "good"
                if self.ecu.do_post_write()
                else "bad"
            )

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="write.result",
                value=self.writeinfo[2]
            )

            ret = 0

        self.do_update_state()

        return ret

    # ---------------------------------------------------------
    # READ EEPROM
    # ---------------------------------------------------------

    def do_read_eeprom(self):

        self.do_update_state()

        ret = 1

        wx.CallAfter(
            dispatcher.send,
            signal="KlineWorker",
            sender=self,
            info="read_eeprom",
            value=None
        )

        self.eeprominfo[2] = self.read_eeprom()

        if self.eeprominfo[2] == "interrupted":

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="read_eeprom.progress",
                value=(
                    0,
                    "interrupted"
                )
            )

        else:

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="read_eeprom.result",
                value=self.eeprominfo[2]
            )

            ret = 0

        self.do_update_state()

        return ret

    # ---------------------------------------------------------
    # WRITE EEPROM
    # ---------------------------------------------------------

    def do_write_eeprom(self):

        self.do_update_state()

        ret = 1

        wx.CallAfter(
            dispatcher.send,
            signal="KlineWorker",
            sender=self,
            info="write_eeprom",
            value=None
        )

        self.eeprominfo[2] = self.write_eeprom(
            self.eeprominfo[1]
        )

        if self.eeprominfo[2] == "interrupted":

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="write_eeprom.progress",
                value=(
                    0,
                    "interrupted"
                )
            )

        else:

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="write_eeprom.result",
                value=self.eeprominfo[2]
            )

            ret = 0

        self.do_update_state()

        return ret

    # ---------------------------------------------------------
    # FORMAT EEPROM
    # ---------------------------------------------------------

    def do_format_eeprom(self, mode):

        self.do_update_state()

        wx.CallAfter(
            dispatcher.send,
            signal="KlineWorker",
            sender=self,
            info="format_eeprom",
            value=None
        )

        if mode == 1:

            status, _ = (
                self.ecu.pgmfi_format_eeprom_FF()
            )

        else:

            status, _ = (
                self.ecu.pgmfi_format_eeprom_00()
            )

        wx.CallAfter(
            dispatcher.send,
            signal="KlineWorker",
            sender=self,
            info="format_eeprom.result",
            value=status
        )

        self.do_update_state()

        return not status

    # ---------------------------------------------------------
    # READ FLASH
    # ---------------------------------------------------------

    def do_read(self):
        print("[KLINE] do_read() ENTERED")

        self.do_update_state()

        ret = 1

        wx.CallAfter(
            dispatcher.send,
            signal="KlineWorker",
            sender=self,
            info="read",
            value=None
        )

        print("[KLINE] Calling read_flash()")
        self.readinfo[2] = self.read_flash()
        print("[KLINE] read_flash() RETURNED:", repr(self.readinfo[2]))

        if self.readinfo[2] == "interrupted":

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="read.progress",
                value=(
                    0,
                    "interrupted"
                )
            )

        else:

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="read.result",
                value=self.readinfo[2]
            )

            ret = 0

        self.do_update_state()

        return ret

    # ---------------------------------------------------------
    # ECU ID
    # ---------------------------------------------------------

    def do_get_ecmid(self):

        ret = 1

        info = self.ecu.send_command(
            [0x72],
            [0x71, 0x00]
        )

        if info:

            self.ecmid = info[2][2:7]

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="ecmid",
                value=bytes(self.ecmid)
            )

            ret = 0

        return ret

    # ---------------------------------------------------------
    # FLASH COUNT
    # ---------------------------------------------------------

    def do_get_flashcount(self):

        ret = 1

        info = self.ecu.send_command(
            [0x7d],
            [0x01, 0x01, 0x03]
        )

        if info:

            self.flashcount = int(info[2][4])

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="flashcount",
                value=self.flashcount
            )

            ret = 0

        return ret

    # ---------------------------------------------------------
    # CLEAR CODES
    # ---------------------------------------------------------

    def do_clear_codes(self):

        print("[KLINE] ========================================")
        print("[KLINE] CLEAR DTC REQUEST")
        print("[KLINE] ========================================")

        try:
            print("[KLINE] Sending clear DTC command...")

            info = self.ecu.send_command(
                [0x72],
                [0x60, 0x03],
                retries=1
            )

            print("[KLINE] Clear DTC response:", repr(info))

            # Clear the request flag
            self.clear_codes = False

            # The ECU has been told to clear the codes.
            # Do NOT set dtccount to -1 because that causes
            # the worker to immediately read DTCs again.
            self.dtccount = 0

            self.errorcodes = {
                "0x74": [],
                "0x73": []
            }

            # Update DTC count in GUI
            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="dtccount",
                value=0
            )

            # Update DTC list in GUI
            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="dtc",
                value=self.errorcodes
            )

            print("[KLINE] DTCs cleared")
            return 1

        except Exception as e:
            import traceback

            print("[KLINE] CLEAR DTC ERROR:", repr(e))
            traceback.print_exc()

            return 0

    # ---------------------------------------------------------
    # GET DTCS
    # ---------------------------------------------------------

    def do_get_dtcs(self):

        errorcodes = {
            "0x74": [],
            "0x73": []
        }

        # 0x74 = current DTCs
        # 0x73 = stored/past DTCs
        for t in [0x74, 0x73]:

            for i in range(1, 0x0c):

                try:

                    info = self.ecu.send_command(
                        [0x72],
                        [t, i],
                        retries=1
                    )

                except Exception:

                    info = None

                if info is None:
                    break

                try:
                    data = info[2]
                except (IndexError, TypeError):
                    break

                # Honda DTC response contains up to
                # three code/subcode pairs.
                for j in [3, 5, 7]:

                    if j + 1 >= len(data):
                        continue

                    code = data[j]
                    subcode = data[j + 1]

                    if code != 0:

                        errorcodes[
                            hex(t)
                        ].append(
                            "%02d-%02d" %
                            (
                                code,
                                subcode
                            )
                        )

                # Byte 2 indicates there are no
                # more DTC records/pages.
                if (
                    len(data) > 2
                    and data[2] == 0
                ):
                    break

        dtccount = sum(
            len(codes)
            for codes in errorcodes.values()
        )

        if self.dtccount != dtccount:

            self.dtccount = dtccount

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="dtccount",
                value=self.dtccount
            )

        if self.errorcodes != errorcodes:

            self.errorcodes = errorcodes

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="dtc",
                value=self.errorcodes
            )

        return 0

    # ---------------------------------------------------------
    # PROBE TABLES
    # ---------------------------------------------------------

    def do_probe_tables(self):

        tables = self.ecu.probe_tables()

        if len(tables) > 0:

            self.tables = tables

            for t, d in self.tables.items():

                wx.CallAfter(
                    dispatcher.send,
                    signal="KlineWorker",
                    sender=self,
                    info="data",
                    value=(
                        t,
                        d[0],
                        d[1]
                    )
                )

            return 0

        else:
            return 1

    # ---------------------------------------------------------
    # UPDATE TABLES
    # ---------------------------------------------------------

    #def do update_state (first)

    # ---------------------------------------------------------
    # BASIC TASKS
    # ---------------------------------------------------------

    def do_basic_tasks(self):

        ret = 0

        if not self.ecmid:
            ret += self.do_get_ecmid()

        return ret

    # ---------------------------------------------------------
    # IDLE TASKS
    # ---------------------------------------------------------

    def do_idle_tasks(self):
        ret = 0

        # Handle Clear DTC request
        if self.clear_codes:
            print("[KLINE] Clear DTC flag detected")
            ret += self.do_clear_codes()


        try:
            if not self.ecmid:
                print("[KLINE] Reading ECU ID...")
                ret += self.do_get_ecmid()
                print("[KLINE] ECU ID complete")

        except Exception as e:
            import traceback
            print("[KLINE] ECU ID ERROR:", repr(e))
            traceback.print_exc()

        try:
            if self.dtccount < 0 or self.update_errors:
                print("[KLINE] Reading DTCs...")
                ret += self.do_get_dtcs()
                print("[KLINE] DTC read complete")
                self.update_errors = False

        except Exception as e:
            import traceback
            print("[KLINE] DTC ERROR:", repr(e))
            traceback.print_exc()

        return ret

    # ---------------------------------------------------------
    # UPDATE ECU STATE
    # ---------------------------------------------------------

    def do_update_state(self):
        if self.ecu is None:
            print("[KLINE] ECU object is None")
            return 0

        print("[KLINE] ========================================")
        print("[KLINE] ECU STATE DETECTION")
        print("[KLINE] ========================================")

        try:
            print("[KLINE] Calling detect_ecu_state()")

            state = self.ecu.detect_ecu_state()

            print(
                "[KLINE] detect_ecu_state() returned:",
                repr(state)
            )

        except Exception as e:
            import traceback
            print("[KLINE] detect_ecu_state() ERROR:", repr(e))
            traceback.print_exc()
            return 0

        if state != self.state:
            print(
                "[KLINE] STATE CHANGE:",
                repr(self.state),
                "->",
                repr(state)
            )

            self.state = state

            wx.CallAfter(
                dispatcher.send,
                signal="KlineWorker",
                sender=self,
                info="state",
                value=self.state
            )

            if self.state == ECUSTATE.OFF:
                self.reset_state()

        if self.state == ECUSTATE.OK:
            print("[KLINE] ECU STATE = OK")
            return 1

        if self.state == ECUSTATE.UNKNOWN:
            time.sleep(2)

        return 0

    # ---------------------------------------------------------
    # PASSWORD
    # ---------------------------------------------------------

    def do_password(self):

        p1 = self.ecu.send_command(
            [0x27],
            [0xe0] + self.password[:7]
        )

        p2 = self.ecu.send_command(
            [0x27],
            [0xe0] + self.password[7:]
        )

        passok = (
            p1 is not None
            and p2 is not None
        )

        wx.CallAfter(
            dispatcher.send,
            signal="KlineWorker",
            sender=self,
            info="password",
            value=passok
        )

        return not passok

    # ---------------------------------------------------------
    # WRITE HELPER
    # ---------------------------------------------------------

    def write_helper(self, init=False, recover=False):

        ret = 1

        if init:

            self.do_init_write(
                recover=recover
            )

            time.sleep(.100)

        if self.do_erase() == 0:

            self.do_write()

            ret = 0

        self.writeinfo = None

        return ret

    # ---------------------------------------------------------
    # READ HELPER
    # ---------------------------------------------------------

    def read_helper(self):
        print("[KLINE] read_helper START:", repr(self.readinfo))
        self.do_read()
        self.readinfo = None
        print("[KLINE] read_helper END")

    # ---------------------------------------------------------
    # EEPROM HELPERS
    # ---------------------------------------------------------

    def read_eeprom_helper(self):

        ret = self.do_read_eeprom()

        self.eeprominfo = None

        return ret

    def write_eeprom_helper(self):

        ret = self.do_write_eeprom()

        self.eeprominfo = None

        return ret

    def format_eeprom_helper(self, mode):

        ret = self.do_format_eeprom(mode)

        self.eeprominfo = None

        return ret

    # ---------------------------------------------------------
    # POWER
    # ---------------------------------------------------------

    def do_on_power(self):

        ret = 0

        if self.sendpassword:

            ret += self.do_password()

            self.sendpassword = False

        return ret

    # ---------------------------------------------------------
    # SECURE
    # ---------------------------------------------------------

    def do_secure(self):

        ret = 1

        if self.state == ECUSTATE.SECURE:

            if self.readinfo is not None:

                ret = self.read_helper()

            elif self.eeprominfo is not None:

                if self.eeprominfo[0] == "read":

                    ret = self.read_eeprom_helper()

                elif self.eeprominfo[0] == "write":

                    ret = self.write_eeprom_helper()

                elif self.eeprominfo[0] == "format":

                    ret = self.format_eeprom_helper(
                        self.eeprominfo[1]
                    )

        return ret

    # ---------------------------------------------------------
    # THREAD
    # ---------------------------------------------------------

    def run(self):
        while self.parent.run:
            if not self.ready:
                time.sleep(.002)
                continue

            try:
                if self.state in [ECUSTATE.UNKNOWN, ECUSTATE.OFF]:
                    ret = self.do_update_state()

                    if ret:
                        print("[KLINE] Legacy initialization successful")
                        self.state = ECUSTATE.OK
                        self.do_on_power()
                    else:
                        time.sleep(.5)

                    continue

                if self.state == ECUSTATE.SECURE:
                    self.do_secure()
                    continue

                if self.state == ECUSTATE.OK:
                    if self.readinfo is not None:
                        print("[KLINE] READ REQUEST DETECTED:", repr(self.readinfo))
                        self.read_helper()
                        self.state = ECUSTATE.OK
                        continue

                    if self.writeinfo is not None:
                        self.write_helper(init=True)
                        self.state = ECUSTATE.OK
                        continue

                    print("[KLINE] LIVE CHECK: update_tables =", repr(self.update_tables), "tables_probed =", repr(self.tables_probed))

                    if self.update_tables and not self.tables_probed:
                        print("[KLINE] ========================================")
                        print("[KLINE] PROBING LIVE DATA TABLES")
                        print("[KLINE] ========================================")

                        try:
                            ret = self.do_probe_tables()

                            print("[KLINE] do_probe_tables() returned:", ret)
                            print("[KLINE] tables:", repr(self.tables))

                            self.tables_probed = True

                        except Exception as e:
                            import traceback
                            print("[KLINE] LIVE DATA PROBE ERROR:", repr(e))
                            traceback.print_exc()
                            self.tables_probed = True

                    ret = self.do_idle_tasks()
                    time.sleep(.25)
                    continue

                if self.state == ECUSTATE.RECOVER_OLD:
                    self.do_basic_tasks()

                    if self.writeinfo is not None:
                        self.write_helper(init=True)
                        self.state = ECUSTATE.OK

                    continue

                if self.state == ECUSTATE.RECOVER_NEW:
                    self.do_basic_tasks()

                    if self.writeinfo is not None:
                        self.write_helper(init=True, recover=True)
                        self.state = ECUSTATE.OK

                    continue

                if self.state == ECUSTATE.WRITE:
                    if self.writeinfo is not None:
                        self.write_helper()

                    self.state = ECUSTATE.OK
                    continue

                time.sleep(.01)

            except USBError as e:
                print("[KLINE] USB ERROR:", repr(e))
                self.__clear_data()

            except FtdiError as e:
                print("[KLINE] FTDI ERROR:", repr(e))
                self.__clear_data()

            except OSError as e:
                print("[KLINE] OS ERROR:", repr(e))
                self.__clear_data()

            except AttributeError as e:
                import traceback
                print("[KLINE] ATTRIBUTE ERROR:", repr(e))
                traceback.print_exc()
                time.sleep(.5)

            except Exception as e:
                import traceback
                print("[KLINE] UNEXPECTED ERROR:", repr(e))
                traceback.print_exc()
                time.sleep(.5)