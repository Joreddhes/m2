from pathlib import Path

from mediahub.media import _finalize_hls_playlists, _hls_is_ready, _write_master_playlist


def test_multi_audio_master_playlist_and_progressive_readiness(tmp_path: Path):
    tracks = [
        {"label": 'Russian "Studio"', "language": "rus"},
        {"label": "Original", "language": "eng"},
    ]
    _write_master_playlist(tmp_path, {"width": 1280, "height": 720}, tracks)

    master = (tmp_path / "index.m3u8").read_text(encoding="utf-8")
    assert master.count("#EXT-X-MEDIA:TYPE=AUDIO") == 2
    assert master.count('CHANNELS="2"') == 2
    assert 'RESOLUTION=1280x720,AUDIO="audio"' in master
    assert 'NAME="Russian \'Studio\'"' in master
    assert not _hls_is_ready(tmp_path, 2)

    playlist = "#EXTM3U\n#EXT-X-PLAYLIST-TYPE:EVENT\n#EXTINF:6.0,\nsegment.ts\n"
    (tmp_path / "video.m3u8").write_text(playlist, encoding="utf-8")
    (tmp_path / "audio-0.m3u8").write_text(playlist, encoding="utf-8")
    assert not _hls_is_ready(tmp_path, 2)
    (tmp_path / "audio-1.m3u8").write_text(playlist, encoding="utf-8")
    assert _hls_is_ready(tmp_path, 2)

    _finalize_hls_playlists(tmp_path)
    assert "PLAYLIST-TYPE:VOD" in (tmp_path / "video.m3u8").read_text(encoding="utf-8")
    assert "PLAYLIST-TYPE:EVENT" not in (tmp_path / "audio-1.m3u8").read_text(encoding="utf-8")
