"""LG Game Setup: the settings bscpylgtv sends, against a fake client and,
when bscpylgtv is installed, through the real library to the fake TV."""
from __future__ import annotations

import asyncio
import importlib.util
import shutil
import socket
import tempfile
import unittest
from pathlib import Path

from lgcal import game


class FakeClient:
    """bscpylgtv's WebOsClient as the setup uses it."""
    def __init__(self, mode="expert2", stored=None, refuse=()):
        self.calls, self.mode, self.stored, self.refuse = [], mode, dict(stored or {}), set(refuse)

    async def set_input(self, input):
        self.calls.append(("set_input", input))

    async def set_device_info(self, input, icon, label):
        self.calls.append(("set_device_info", input, icon, label))

    async def set_system_settings(self, category, settings, current_app=None):
        if self.refuse & set(settings):
            raise RuntimeError("Some keys are not allowed for the request.")
        self.calls.append(("public", category, settings, current_app))
        self.stored.update({k: v for k, v in settings.items() if k not in self.stored})

    async def set_settings(self, category, settings, current_app=None):
        self.calls.append(("internal", category, settings))
        if "pictureMode" in settings:
            self.mode = settings["pictureMode"]

    async def button(self, name):
        self.calls.append(("button", name))

    async def enable_tpc_or_gsr(self, algo, enable=True):
        self.calls.append(("tpc_gsr", algo, enable))

    async def get_system_settings(self, category, keys):
        values = {"pictureMode": self.mode, **self.stored}
        return {"settings": {k: values[k] for k in keys if k in values}}


def run(client):
    said = []
    missed = asyncio.run(game.setup(client, 2, said.append, "amd", sleep=lambda _s: asyncio.sleep(0)))
    return missed, "\n".join(said), client.calls


def sent(calls, route):
    return {k: v for c in calls if c[0] == route for k, v in c[2].items()}


class Setup(unittest.TestCase):
    def test_the_routes_and_order_of_bscpylgtvs_own_g2_scripts(self):
        missed, text, calls = run(FakeClient())
        self.assertEqual(missed, [], text)
        self.assertEqual(calls[:2], [("set_input", "HDMI_2"), ("set_device_info", "HDMI_2", "pc", "PC")])
        self.assertEqual(calls[2:4], [("internal", "picture", {"pictureMode": "game"}), ("button", "ENTER")])
        public, internal = sent(calls, "public"), sent(calls, "internal")
        self.assertEqual((public["gamma"], public["colorTemperature"], public["contrast"], public["brightness"]),
                         ("medium", "-45", "85", "49"))
        self.assertIn(("public", "picture", {"truMotionMode": "off"}, True), calls)
        self.assertEqual((internal["inputOptimization"], internal["gameOptimizationHDMI2"],
                          internal["freesyncOLEDHDMI2"], internal["gameMode"], internal["logoLuminanceAdjust"]),
                         ("on", "off", "on", {"hdmi2": "on"}, "off"))   # AMD: FreeSync, not VRR
        self.assertEqual(public["adjustingLuminance"], [5] + [0] * 21)       # 2.5% +5
        # every internal write is followed by ENTER, as the G2 scripts do
        for i, call in enumerate(calls):
            if call[0] == "internal":
                self.assertEqual(calls[i + 1], ("button", "ENTER"))
        self.assertIn(("tpc_gsr", "tpc", False), calls)
        self.assertIn(("tpc_gsr", "gsr", False), calls)

    def test_hdr_game_mode_gets_the_hdr_column(self):
        _missed, _text, calls = run(FakeClient(mode="hdrGame"))
        public, internal = sent(calls, "public"), sent(calls, "internal")
        self.assertEqual((public["hdrDynamicToneMapping"], public["contrast"], public["peakBrightness"],
                          internal["freesync"], internal["gameOptimization"]), ("HGIG", "100", "high", "on", "off"))
        self.assertNotIn("adjustingLuminance", public)
        self.assertNotIn("pictureMode", internal)

    def test_a_refused_picture_setting_takes_the_internal_route_and_a_kept_one_is_reported(self):
        missed, _text, calls = run(FakeClient(stored={"sharpness": "10"}, refuse={"screenShift"}))
        self.assertEqual(sent(calls, "internal")["screenShift"], "off")
        self.assertIn('Sharpness (the TV has "10")', missed)

    def test_everything_on_the_sheet_is_sent(self):
        _missed, _text, calls = run(FakeClient())
        public, internal = sent(calls, "public"), sent(calls, "internal")
        for key in ("tint", "filmMakerMode", "screenShift", "aspectRatio", "justScan"):
            self.assertIn(key, public)
        for key in ("oledCareMode", "lgLogoDisplay", "quickStartMode", "livePlus", "homeAutoLaunch",
                    "gameGenre", "blackStabilizer", "whiteStabilizer", "lowLevelAdjustment", "darkMode",
                    "blueLight", "enableALLM", "inputOptimization", "ai_Picture", "ai_Brightness", "ai_Genre"):
            self.assertIn(key, internal)

    def test_graphics_card_from_windows(self):
        class Done:
            def __init__(self, names):
                self.stdout = names
        for names, vendor in (("NVIDIA GeForce RTX 4080\nNVIDIA GeForce GT 710", "nvidia"),
                              ("AMD Radeon RX 7900 XTX", "amd"),
                              ("NVIDIA GeForce RTX 4080\nAMD Radeon(TM) Graphics", "")):
            self.assertEqual(game.gpu_vendor(run=lambda *_a, **_k: Done(names)), vendor)


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


@unittest.skipUnless(importlib.util.find_spec("bscpylgtv") and port_free(3001), "needs bscpylgtv and port 3001")
class RealLibrary(unittest.TestCase):
    def test_bscpylgtv_puts_the_settings_on_the_fake_tv(self):
        from tests.fake_webos import KEY, FakeWebOS
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        tv = FakeWebOS(directory)
        self.addCleanup(tv.close)
        from unittest import mock
        from bscpylgtv import WebOsClient

        async def enter(self, name, checkValid=True):
            pass                                     # the fake TV has no pointer-input socket
        said = []
        with mock.patch.object(WebOsClient, "button", enter):
            missed = game.run("127.0.0.1", str(directory / "keys.sqlite"), 2, said.append, client_key=KEY,
                              vendor=lambda: "amd")
        self.assertEqual(missed, [], "\n".join(said))
        self.assertEqual((tv.input, tv.devices["HDMI_2"]), ("HDMI_2", "pc.png"))
        other, picture = tv.settings["other"], tv.settings["picture"]
        self.assertEqual((picture["pictureMode"], other["gameMode"]["hdmi2"], other["freesyncOLEDHDMI2"],
                          other["inputOptimization"], picture["gamma"], picture["truMotionMode"]),
                         ("game", "on", "on", "on", "medium", "off"))
        self.assertEqual(tv.settings["aiPicture"], {"ai_Picture": "off", "ai_Brightness": "off", "ai_Genre": "off"})


if __name__ == "__main__":
    unittest.main()
