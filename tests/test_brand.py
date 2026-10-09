"""Tests for the user-supplied brand (brand/brand.json). Run with: python3 -m unittest discover tests"""

import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brand  # noqa: E402

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'


class BrandTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        brand.set_brand_dir(self.dir)

    def write(self, manifest, files=()):
        for name in files:
            with open(os.path.join(self.dir, name), "wb") as f:
                f.write(SVG)
        with open(os.path.join(self.dir, "brand.json"), "w") as f:
            f.write(manifest if isinstance(manifest, str) else json.dumps(manifest))
        brand.set_brand_dir(self.dir)
        return brand.load()

    def test_no_brand_directory(self):
        brand.set_brand_dir(os.path.join(self.dir, "missing"))
        self.assertIsNone(brand.load()["brand"])
        self.assertEqual(brand.describe(), [])

    def test_valid_brand(self):
        info = self.write({"name": "Example Co", "logo": "logo.svg", "accentColor": "#7C5CFF", "url": "https://example.com"}, ["logo.svg"])
        b = info["brand"]
        self.assertEqual(b["name"], "Example Co")
        self.assertTrue(b["logo"].startswith("/brand/logo.svg?v="))
        self.assertEqual(b["accentColor"], "#7c5cff")
        self.assertEqual(info["problems"], [])
        self.assertEqual(brand.asset("logo.svg")[1], "image/svg+xml")

    def test_only_listed_files_are_served(self):
        self.write({"name": "X", "logo": "logo.svg"}, ["logo.svg", "other.svg"])
        self.assertIsNone(brand.asset("other.svg"))
        self.assertIsNone(brand.asset("brand.json"))
        self.assertIsNone(brand.asset("../brand.json"))

    def test_unsafe_values_are_rejected(self):
        info = self.write({"name": "X", "logo": "../etc/passwd.png", "favicon": "f.exe", "accentColor": "red",
                           "url": "javascript:alert(1)"})
        self.assertEqual(set(info["brand"]), {"name"})
        self.assertEqual(len(info["problems"]), 4)

    def test_name_is_required_and_bad_json_is_reported(self):
        self.assertIsNone(self.write({"logo": "logo.svg"}, ["logo.svg"])["brand"])
        info = self.write("{not json")
        self.assertIsNone(info["brand"])
        self.assertIn("could not read brand.json", info["problems"][0])

    def test_oversized_logo_is_ignored(self):
        with open(os.path.join(self.dir, "big.png"), "wb") as f:
            f.write(b"\0" * (brand.MAX_ASSET_BYTES + 1))
        info = self.write({"name": "X", "logo": "big.png"})
        self.assertNotIn("logo", info["brand"])

    def test_edits_apply_without_restart(self):
        self.write({"name": "First"})
        self.assertEqual(brand.load()["brand"]["name"], "First")
        path = os.path.join(self.dir, "brand.json")
        with open(path, "w") as f:
            json.dump({"name": "Second"}, f)
        os.utime(path, ns=(1, os.stat(path).st_mtime_ns + 1_000_000))
        self.assertEqual(brand.load()["brand"]["name"], "Second")


if __name__ == "__main__":
    unittest.main()
