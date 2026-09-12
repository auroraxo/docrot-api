"""T14: link extraction across MD/MDX/RST/HTML with line numbers."""

import unittest

from docrot_scan_api import links


class MarkdownExtractionTests(unittest.TestCase):
    def test_inline_links_and_images_with_lines(self):
        text = ("# Title\n"
                "[broken](https://bad.test/gone)\n"
                "![logo](https://img.test/logo.png)\n")
        result = links.extract_markdown(text)
        self.assertIn(("https://bad.test/gone", 2), result)
        self.assertIn(("https://img.test/logo.png", 3), result)

    def test_angle_and_bare_autolinks(self):
        text = "<https://angle.test/x>\nsee https://bare.test/page now\n"
        result = links.extract_markdown(text)
        self.assertIn(("https://angle.test/x", 1), result)
        self.assertIn(("https://bare.test/page", 2), result)

    def test_reference_definition(self):
        text = "[docs]: https://ref.test/home\nuse [docs][docs]\n"
        result = links.extract_markdown(text)
        self.assertIn(("https://ref.test/home", 1), result)

    def test_non_http_ignored(self):
        text = ("[mail](mailto:a@b.c)\n"
                "[ftp](ftp://files.test/x)\n"
                "[rel](./other.md)\n"
                "[anchor](#section)\n"
                "[proto-relative](//cdn.test/x.js)\n")
        self.assertEqual(links.extract_markdown(text), [])

    def test_no_duplicates_within_inline(self):
        text = "[a](https://once.test/x)\n"
        result = links.extract_markdown(text)
        self.assertEqual(result, [("https://once.test/x", 1)])

    def test_trailing_punctuation_trimmed(self):
        text = "go to https://sentence.test/page.\n"
        result = links.extract_markdown(text)
        self.assertEqual(result, [("https://sentence.test/page", 1)])

    def test_mdx_html_attributes(self):
        text = 'import X from "../x"\n\n<a href="https://mdx.test/page">y</a>\n'
        result = links.extract_markdown(text)
        self.assertIn(("https://mdx.test/page", 3), result)


class RstExtractionTests(unittest.TestCase):
    def test_named_and_anonymous_links(self):
        text = ("Named `docs <https://rst.test/docs>`_\n"
                "Anon `click <https://rst.test/anon>`__\n")
        result = links.extract_rst(text)
        self.assertIn(("https://rst.test/docs", 1), result)
        self.assertIn(("https://rst.test/anon", 2), result)

    def test_image_directive(self):
        text = ".. image:: https://img.test/pic.png\n"
        result = links.extract_rst(text)
        self.assertEqual(result, [("https://img.test/pic.png", 1)])

    def test_role_and_target(self):
        text = (":ref:`core <https://role.test/ref>`\n"
                ".. _ homepage: https://target.test/\n")
        result = links.extract_rst(text)
        self.assertIn(("https://role.test/ref", 1), result)
        self.assertIn(("https://target.test/", 2), result)

    def test_bare_url(self):
        text = "Browse https://rst-bare.test/page today\n"
        result = links.extract_rst(text)
        self.assertEqual(result, [("https://rst-bare.test/page", 1)])

    def test_non_http_ignored(self):
        text = "`mail <mailto:a@b.c>`_\n.. image:: img/local.png\n"
        self.assertEqual(links.extract_rst(text), [])


class HtmlExtractionTests(unittest.TestCase):
    def test_href_src_with_lines(self):
        text = ('<a href="https://h.test/page">x</a>\n'
                '<img src="https://h.test/i.png">\n')
        result = links.extract_html(text)
        self.assertIn(("https://h.test/page", 1), result)
        self.assertIn(("https://h.test/i.png", 2), result)

    def test_meta_refresh(self):
        text = '<meta http-equiv="refresh" content="0; url=https://h.test/r">\n'
        result = links.extract_html(text)
        self.assertIn(("https://h.test/r", 1), result)

    def test_relative_and_anchor_ignored(self):
        text = '<a href="/local">x</a><a href="#top">y</a>'
        self.assertEqual(links.extract_html(text), [])


class DispatchTests(unittest.TestCase):
    def test_extension_dispatch(self):
        self.assertEqual(links.extract_links("a.md", "[x](https://k.test/1)"),
                         [("https://k.test/1", 1)])
        self.assertEqual(links.extract_links("a.markdown", "[x](https://k.test/2)"),
                         [("https://k.test/2", 1)])
        self.assertEqual(links.extract_links("a.mdx", "[x](https://k.test/3)"),
                         [("https://k.test/3", 1)])
        self.assertEqual(links.extract_links("a.htm", '<a href="https://k.test/4">'),
                         [("https://k.test/4", 1)])
        self.assertEqual(links.extract_links("a.py", "https://k.test/5"), [])

    def test_case_insensitive_extension(self):
        self.assertEqual(links.extract_links("README.MD", "[x](https://k.test/6)"),
                         [("https://k.test/6", 1)])


if __name__ == "__main__":
    unittest.main()
