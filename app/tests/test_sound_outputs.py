import unittest

from framewise import sound_outputs


def sink(i, name, desc, device=None, profile_device=None):
    props = {"media.class": "Audio/Sink", "node.name": name, "node.description": desc}
    if device is not None:
        props.update({"device.id": device, "card.profile.device": profile_device})
    return {"id": i, "type": "PipeWire:Interface:Node", "info": {"props": props}}


CHIP = "Meteor Lake-P HD Audio Controller"
PCI = "alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__"


class OutputTests(unittest.TestCase):
    def test_like_a_laptop_with_a_screen_and_headphones(self):
        card = {"id": 51, "type": "PipeWire:Interface:Device", "info": {"params": {"EnumRoute": [
            {"direction": "Output", "available": "no", "devices": [0]},      # HDMI 3: nothing plugged in
            {"direction": "Output", "available": "yes", "devices": [2]},     # HDMI 1: the screen
            {"direction": "Output", "available": "unknown", "devices": [3]},  # speaker
        ]}}}
        objects = [card,
                   sink(58, PCI + "HDMI3__sink", f"{CHIP} HDMI / DisplayPort 3 Output", 51, 0),
                   sink(60, PCI + "HDMI1__sink", f"{CHIP} HDMI / DisplayPort 1 Output", 51, 2),
                   sink(61, PCI + "Speaker__sink", f"{CHIP} Speaker", 51, 3),
                   sink(144, "bluez_output.84_9D", "soundcore Space 2"),
                   {"id": 7, "type": "PipeWire:Interface:Node", "info": {"props": {"media.class": "Stream/Output/Audio"}}}]
        self.assertEqual(sound_outputs.parse(objects), [
            (PCI + "HDMI1__sink", "HDMI / DisplayPort 1 Output"),
            (PCI + "Speaker__sink", "Speaker"),
            ("bluez_output.84_9D", "soundcore Space 2"),
        ])

    def test_short_names(self):
        self.assertEqual(sound_outputs.short_names([f"{CHIP} Speaker", f"{CHIP} Headphones"]),
                         ["Speaker", "Headphones"])
        self.assertEqual(sound_outputs.short_names(["One device"]), ["One device"])

    def test_nothing(self):
        self.assertEqual(sound_outputs.parse([]), [])


if __name__ == "__main__":
    unittest.main()
