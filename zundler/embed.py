"""
Embed HTML assets.

It creates an HTML file that has three script tags:

1. A virtual file tree containing all assets in zipped form
2. The pako JS library to unzip the assets
3. Some boostrap code that fixes the HTML so it loads all assets from the
virtual file tree instead of the file system

Also, two scripts are injected into all HTML files in the file tree. One as
the first child of <head>, one as the last child of <body>. The first does some
monkeypatching, the last sets up all magic.

Author: Adrian Vollmer

"""

import base64
from fnmatch import fnmatch
import json
import logging
import os
from pathlib import Path
import re
import zlib

try:
    import magic
except ImportError as e:
    logger = logging.getLogger(__name__)
    logger.error(str(e))
    logger.warning("Using `mimetypes` instead of `python-magic` for mime type guessing")
    import mimetypes


SCRIPT_PATH = os.path.abspath(os.path.dirname(__file__))

logger = logging.getLogger(__name__)


def embed_assets(index_file, output_path=None, append_pre="", append_post=""):
    init_files = {}
    for filename in [
        "init.css",
        "init.html",
        "bootstrap.js",
        "main.js",
        "inject_pre.js",
        "inject_post.js",
        "pako.min.js",
        "LICENSE",
    ]:
        path = os.path.join(SCRIPT_PATH, "assets", filename)
        init_files[filename] = open(path, "r").read()

    if not os.path.exists(index_file):
        raise FileNotFoundError("no such file: %s" % index_file)

    base_dir = os.path.dirname(index_file)
    base_name = os.path.basename(index_file)
    new_base_name = "SELF_CONTAINED_" + base_name

    if not output_path:
        output_path = os.path.join(base_dir, new_base_name)

    before = init_files["inject_pre.js"] + append_pre
    after = init_files["inject_post.js"] + append_post
    file_tree = load_filetree(
        base_dir,
        before=before,
        after=after,
        exclude_pattern=new_base_name,
    )

    remote_resources = []

    global_context = {
        "current_path": base_name,
        "file_tree": file_tree,
        "remote_resources": remote_resources,
        "main": init_files["main.js"],
    }

    global_context = json.dumps(global_context)
    logger.debug("total asset size: %d" % len(global_context))
    global_context = deflate(global_context)
    logger.debug("total asset size (compressed): %d" % len(global_context))

    result = """
<!DOCTYPE html>
<html>
<head><style>{style}</style></head>
<body>{body}
<script>window.global_context = "{global_context}"</script>
<script>{pako} //# sourceURL=pako.js</script>
<script>{bootstrap} //# sourceURL=boostrap.js</script>
</body><!-- {license} --></html>
""".format(
        style=init_files["init.css"],
        body=init_files["init.html"],
        pako=init_files["pako.min.js"],
        bootstrap=init_files["bootstrap.js"],
        global_context=global_context,
        license=init_files["LICENSE"],
    )

    with open(output_path, "w") as fp:
        fp.write(result)

    logger.info("Result written to: %s" % output_path)
    return output_path


def prepare_file(filename, before, after):
    """Prepare a file for the file tree

    Referenced assets in CSS files will be embedded.
    HTML files will be injected with two scripts.

    `filename`: The name of the file
    `before`: Javascript code that will be inserted as the first child of
        `<body>` if the file is HTML.
    `after`: Javascript code that will be inserted as the last child of
        `<body>` if the file is HTML.

    """
    _, ext = os.path.splitext(filename)
    ext = ext.lower()[1:]
    data = open(filename, "rb").read()
    mime_type = mime_type_from_bytes(filename, data)
    base64encoded = False

    if ext == "css":
        # assuming all CSS files have names ending in '.css'
        data = embed_css_resources(data, filename)

    elif ext in [
        "png",
        "jpg",
        "jpeg",
        "woff",
        "woff2",
        "eot",
        "ttf",
        "gif",
        "ico",
    ]:
        # JSON doesn't allow binary data
        data = base64.b64encode(data)
        base64encoded = True

    elif ext in ["html", "htm"]:
        data = embed_html_resources(
            data,
            os.path.dirname(filename),
            before,
            after,
        ).encode()

    if not isinstance(data, str):
        try:
            data = data.decode()
        except UnicodeError:
            data = base64.b64encode(data).decode()

    logger.debug("loaded file: %s [%s, %d bytes]" % (filename, mime_type, len(data)))

    result = {
        "data": data,
        "mime_type": mime_type,
        "base64encoded": base64encoded,
    }

    return result


def deflate(data):
    data = zlib.compress(data.encode())
    data = base64.b64encode(data).decode()
    return data


def embed_html_resources(html, base_dir, before, after):
    """Embed fonts in preload links to avoid jumps when loading"""
    # This cannot be done in JavaScript, it would be too late

    import bs4

    soup = bs4.BeautifulSoup(html, "lxml")
    body = soup.find("body")
    head = soup.find("head")

    if head and before:
        script = soup.new_tag("script")
        script.string = before + "//# sourceURL=inject_pre.js"
        head.insert(0, script)

    if body and after:
        script = soup.new_tag("script")
        script.string = after + "//# sourceURL=inject_post.js"
        body.append(script)

    # TODO embed remote resources in case we want the entire file to be
    # usable in an offline environment

    return str(soup)


def to_data_uri(filename, mime_type=None):
    """Create a data URI from the contents of a file"""

    try:
        data = open(filename, "br").read()
    except FileNotFoundError as e:
        logger.error(str(e))
    data = base64.b64encode(data)
    if not mime_type:
        mime_type = "application/octet-stream"
    return "data:%s;charset=utf-8;base64, %s" % (
        mime_type,
        data.decode(),
    )


def embed_css_resources(css, filename):
    """Replace `url(<path>)` with `url(data:<mime_type>;base64, ...)`

    Also, handle @import."""
    # This uses some heuristics which will fail in general.
    # Eventually a library like tinycss2 might be preferable.

    # First, make sure all @import's are using url(), because these are both valid:
    # @import url("foo.css");
    # @import "foo.css";
    regex = rb"""(?P<rule>@import\s*['"]?(?P<url>.*?)['"]?\s*;)"""
    replace_rules = {}
    for m in re.finditer(regex, css, flags=re.IGNORECASE):
        if not m["url"].lower().startswith(b"url("):
            replace_rules[m["rule"]] = b"@import url('%s');" % m["url"]
    for orig, new in replace_rules.items():
        css = css.replace(orig, new)

    # Quotes are optional. But then URLs can contain escaped characters.
    regex = (
        rb"""(?P<url_statement>url\(['"]?(?P<url>.*?)['"]?\))"""
        rb"""(\s*format\(['"](?P<format>.*?)['"]\))?"""
    )

    replace_rules = {}

    for m in re.finditer(regex, css, flags=re.IGNORECASE):
        if re.match(b"""['"]?data:.*""", m["url"]):
            continue

        path = m["url"].decode()

        if "?" in path:
            path = path.split("?")[0]
        if "#" in path:
            path = path.split("#")[0]

        path = os.path.dirname(filename) + "/" + path

        try:
            content = open(path, "rb").read()
        except FileNotFoundError as e:
            logger.error(str(e))
            continue

        # If it's binary, determine mime type and encode in base64
        if m["format"]:
            mime_type = "font/" + m["format"].decode()
        elif path[-3:].lower() == "eot":
            mime_type = "font/eot"
        elif path[-3:].lower() == "css":
            mime_type = "text/css"
            content = embed_css_resources(content, filename)
        else:
            mime_type = mime_type_from_bytes(filename, content)
        if not mime_type:
            logger.error("Unable to determine mime type: %s" % path)
            mime_type = "application/octet-stream"
        content = base64.b64encode(content)

        replace_rules[m["url_statement"]] = (
            b'url("data:%(mime_type)s;charset=utf-8;base64, %(content)s")'
            % {
                b"content": content,
                b"mime_type": mime_type.encode(),
            }
        )

    for orig, new in replace_rules.items():
        css = css.replace(orig, new)

    return css


def mime_type_from_bytes(filename, buffer):
    try:
        mime_type = magic.Magic(mime=True).from_buffer(buffer)
    except NameError:
        mime_type = mimetypes.guess_type(filename)[0]

    if not mime_type:
        logger.error(
            "Unknown mime type (%s): %s" % (filename, str(buffer[:10]) + "...")
        )
        mime_type = "application/octet-stream"

    return mime_type


def load_filetree(base_dir, before=None, after=None, exclude_pattern=None):
    """Load entire directory in a dict"""

    result = {}
    base_dir = Path(base_dir)
    for path in base_dir.rglob("*"):
        if exclude_pattern and fnmatch(path.name, exclude_pattern):
            continue
        if path.is_file():
            key = path.relative_to(base_dir).as_posix()
            result[key] = prepare_file(
                path.as_posix(),
                before,
                after,
            )
            logger.debug("Packed file %s [%d]" % (key, len(result[key])))

    return result


def extract_assets(input_path, output_path=None):
    """Split a file generated by Zundler into its constituents

    Import for debugging"""

    if not output_path:
        output_path = "."

    html = open(input_path, "r").read()

    try:
        # Find large base64 blob
        m = re.search(
            '.*<script>.*window.*"(?P<blob>[A-Za-z0-9/+]{128,})".*</script>.*', html
        )
        blob = m["blob"]
        blob = base64.b64decode(blob)
        blob = zlib.decompress(blob).decode()
        blob = json.loads(blob)
        file_tree = blob["file_tree"]
    except Exception as e:
        logger.error(str(e))
        logger.error("Does not look like a Zundler output file: %s" % input_path)
        exit(1)

    for filename, file in file_tree.items():
        filename = os.path.join(output_path, filename)
        os.makedirs(os.path.dirname(filename) or ".", exist_ok=True)
        data = file["data"]
        if file["base64encoded"]:
            data = base64.b64decode(data)
        else:
            data = data.encode()
        open(filename, "wb").write(data)
        file["data"] = file["data"][:100] + "..."

    with open(os.path.join(output_path, "file_tree.json"), "w") as fp:
        json.dump(file_tree, fp, indent=2)
