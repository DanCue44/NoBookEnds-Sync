from nobookends_sync.abs_client import ebook_file, file_signature


def _item(main_fmt, extra=()):
    files = [{"ino": "m", "fileType": "ebook", "metadata": {"ext": f".{main_fmt}", "size": 9, "mtimeMs": 1}}]
    files += [{"ino": i, "fileType": t, "isSupplementary": True,
               "metadata": {"ext": e, "size": 7, "mtimeMs": 2}} for i, e, t in extra]
    return {
        "id": "li",
        "media": {"ebookFile": {"ino": "m", "ebookFormat": main_fmt,
                                "metadata": {"ext": f".{main_fmt}", "size": 9, "mtimeMs": 1}}},
        "libraryFiles": files,
    }


def test_main_epub_is_used_as_is():
    eb = ebook_file(_item("epub", [("x", ".epub", "ebook")]))
    assert eb["ino"] == "m" and eb["format"] == "epub"
    assert eb["path"] == "/api/items/li/ebook"


def test_epub_next_to_a_kindle_file_is_preferred():
    eb = ebook_file(_item("azw3", [("cover", ".jpg", "image"), ("e2", ".epub", "ebook")]))
    assert eb["ino"] == "e2" and eb["format"] == "epub"
    assert eb["path"] == "/api/items/li/ebook/e2"


def test_kindle_file_alone_stays_the_choice():
    eb = ebook_file(_item("azw3", [("cover", ".jpg", "image")]))
    assert eb["ino"] == "m" and eb["format"] == "azw3"


def test_minified_items_without_files_use_the_main_ebook():
    item = _item("mobi")
    del item["libraryFiles"]
    assert ebook_file(item)["ino"] == "m"


def test_signature_follows_the_chosen_file():
    audio = {"media": {"tracks": [], "audioFiles": []}}
    alone = file_signature(audio, _item("azw3"))
    with_epub = file_signature(audio, _item("azw3", [("e2", ".epub", "ebook")]))
    assert alone != with_epub
