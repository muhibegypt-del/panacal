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
    def __init__(self, mode="expert2", stored=None):
        self.calls, self.mode, self.stored = [], mode, dict(stored or {})

    async def set_input(self, input):
        self.calls.append(("set_input", input))

    async def set_device_info(self, input, icon, label):
        self.calls.append(("set_device_info", input, icon, label))

    async def set_settings(self, category, settings, current_app=None):
        self.calls.append(("set_settings", category, settings, current_app))
        if settings.get("gameMode") and self.mode != "hdrGame":
            self.mode = "game"
        self.stored.update({k: v for k, v in settings.items() if k not in self.stored or k == "pictureMode"})

    async def set_current_picture_mode(self, mode):
        self.mode = mode

    async def enable_tpc_or_gsr(self, algo, enable=True):
        self.calls.append(("tpc_gsr", algo, enable))

    async def get_system_settings(self, category, keys):
        values = {"pictureMode": self.mode, **self.stored}
        return {"settings": {k: values[k] for k in keys if k in values}}


def run(client, gpu="nvidia"):
    said = []
    missed = asyncio.run(game.setup(client, 2, gpu, said.append, sleep=lambda _s: asyncio.sleep(0)))
    return missed, "\n".join(said), client.calls


class Setup(unittest.TestCase):
    def test_pc_mode_game_optimizer_and_every_setting_in_one_go(self):
        missed, text, calls = run(FakeClient())
        self.assertEqual(missed, [], text)
        self.assertEqual(calls[:3], [("set_input", "HDMI_2"), ("set_device_info", "HDMI_2", "pc", "PC"),
                                     ("set_settings", "other", {"gameMode": {"hdmi2": "on"}}, None)])
        sent = {k: v for c in calls if c[0] == "set_settings" for k, v in c[2].items()}
        self.assertEqual((sent["inputOptimization"], sent["gameOptimizationHDMI2"], sent["freesyncOLEDHDMI2"],
                          sent["gamma"], sent["colorTemperature"], sent["contrast"], sent["brightness"]),
                         ("on", "on", "off", "medium", -45, 85, 49))
        self.assertNotIn("hdrDynamicToneMapping", sent)
        self.assertIn(("set_settings", "picture", {"truMotionMode": "off"}, True), calls)
        self.assertIn(("tpc_gsr", "tpc", False), calls)
        self.assertIn(("tpc_gsr", "gsr", False), calls)
        self.assertTrue(all(len(c[2]) == 1 for c in calls if c[0] == "set_settings"))   # one setting per call

    def test_hdr_game_mode_gets_the_hdr_column_and_amd_gets_freesync(self):
        _missed, _text, calls = run(FakeClient(mode="hdrGame"), gpu="amd")
        sent = {k: v for c in calls if c[0] == "set_settings" for k, v in c[2].items()}
        self.assertEqual((sent["hdrDynamicToneMapping"], sent["contrast"], sent["peakBrightness"],
                          sent["freesyncOLEDHDMI2"], sent["gameOptimization"]), ("HGIG", 100, "high", "on", "off"))

    def test_a_setting_the_tv_kept_is_reported(self):
        missed, _text, _calls = run(FakeClient(stored={"blackStabilizer": 13}))
        self.assertEqual(missed, ["Black Stabiliser (the TV has 13)"])

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
        said = []
        missed = game.run("127.0.0.1", KEY, 2, said.append, vendor=lambda: "amd")
        self.assertEqual(missed, [], "\n".join(said))
        self.assertEqual((tv.input, tv.devices["HDMI_2"]), ("HDMI_2", "pc.png"))
        other, picture = tv.settings["other"], tv.settings["picture"]
        self.assertEqual((picture["pictureMode"], other["gameMode"]["hdmi2"], other["freesyncOLEDHDMI2"],
                          other["inputOptimization"], picture["gamma"], picture["truMotionMode"]),
                         ("game", "on", "on", "on", "medium", "off"))
        self.assertEqual(tv.settings["aiPicture"], {"ai_Picture": "off", "ai_Brightness": "off", "ai_Genre": "off"})


if __name__ == "__main__":
    unittest.main()
