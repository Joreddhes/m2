from pathlib import Path

import pytest

from mediahub.storage import LocalStorage, safe_component, safe_relative


@pytest.mark.parametrize("value", ["../secret", "/absolute", "folder/../../secret", ""])
def test_rejects_unsafe_relative_paths(value):
    with pytest.raises(ValueError):
        safe_relative(value)


def test_local_storage_roundtrip(tmp_path: Path):
    storage = LocalStorage(str(tmp_path))
    storage.mkdir("video/Сериал/Сезон 1")
    storage.write_bytes("video/Сериал/Сезон 1/Серия 1.mp4", b"video")

    assert storage.read_bytes("video/Сериал/Сезон 1/Серия 1.mp4") == b"video"
    assert [entry.name for entry in storage.list("video/Сериал")] == ["Сезон 1"]
    assert [entry.name for entry in storage.walk_files("video/Сериал")] == ["Серия 1.mp4"]


def test_safe_component_preserves_cyrillic_and_rejects_reserved_names():
    assert safe_component("Серия 01.mkv") == "Серия 01.mkv"
    with pytest.raises(ValueError):
        safe_component("CON.txt")
