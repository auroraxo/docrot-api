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


class EntityDecodingTests(unittest.TestCase):
    """HTML entities in URLs must be decoded before checking (scanner v5 class).

    Browsers decode attribute values before dispatching the request; a checker
    that live-tests the raw entity literal gets false positives (docrot v1.2.0).
    """

    def test_amp_in_markdown_url(self):
        text = "![badge](https://cdn.test/x?a=1&amp;b=2)\n"
        self.assertEqual(links.extract_markdown(text),
                         [("https://cdn.test/x?a=1&b=2", 1)])

    def test_hex_and_named_entities_in_html_src(self):
        text = '<img src="https://cdn.test/i.png?v&#x3D;4&amp;s&#x3D;18">\n'
        self.assertEqual(links.extract_html(text),
                         [("https://cdn.test/i.png?v=4&s=18", 1)])

    def test_entity_in_html_attr_inside_markdown(self):
        text = '<a href="https://ex.test/p?a=1&amp;b=2">x</a>\n'
        self.assertEqual(links.extract_markdown(text),
                         [("https://ex.test/p?a=1&b=2", 1)])

    def test_no_entity_output_verbatim(self):
        # plain URLs with a literal & stay untouched by unescape
        text = "[x](https://ex.test/p?a=1&b=2)\n"
        self.assertEqual(links.extract_markdown(text),
                         [("https://ex.test/p?a=1&b=2", 1)])

    def test_line_numbers_survive_decoding(self):
        text = "# h\n\n![b](https://c.test/i.png?x&#x3D;1)\n"
        self.assertEqual(links.extract_markdown(text),
                         [("https://c.test/i.png?x=1", 3)])


class CodeExclusionTests(unittest.TestCase):
    """Fenced/inline code is not rendered; live-checking its URLs would be a false positive."""

    def test_fenced_code_urls_excluded(self):
        text = ("[real](https://a.test/x)\n"
                "```python\n"
                "[demo](https://bad.test/demo)\n"
                "```\n")
        self.assertEqual(links.extract_markdown(text),
                         [("https://a.test/x", 1)])

    def test_tilde_fence_excluded(self):
        text = ("~~~\n"
                "https://bad.test/tilde\n"
                "~~~\n"
                "[ok](https://a.test/y)\n")
        self.assertEqual(links.extract_markdown(text),
                         [("https://a.test/y", 4)])

    def test_unclosed_fence_extends_to_end(self):
        text = ("[real](https://a.test/x)\n"
                "```bash\n"
                "curl https://bad.test/late\n")
        self.assertEqual(links.extract_markdown(text),
                         [("https://a.test/x", 1)])

    def test_inline_code_span_blanked(self):
        text = "run `see https://bad.test/x` then [ok](https://a.test/y)\n"
        self.assertEqual(links.extract_markdown(text),
                         [("https://a.test/y", 1)])

    def test_line_numbers_preserved_after_stripping(self):
        text = ("intro\n"
                "\n"
                "```md\n"
                "![x](https://bad.test/i.png)\n"
                "\n"
                "more\n"
                "```\n"
                "\n"
                "[live](https://a.test/z)\n")
        self.assertEqual(links.extract_markdown(text),
                         [("https://a.test/z", 9)])

    def test_rst_backticks_untouched(self):
        text = "See `Docs <https://rst.example/page>`_ now\n"
        self.assertEqual(links.extract_rst(text),
                         [("https://rst.example/page", 1)])
