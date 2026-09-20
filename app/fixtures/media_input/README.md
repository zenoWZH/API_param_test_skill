The original `manifest.json` describes generated color squares and flite speech.
`real_manifest.json` adds actual NASA spacecraft photographs and a short human
voice recording. Both manifests contain payload hashes and exact expectations.

Real-content fixtures:

- `photo_earth.jpeg`: Apollo 17's photograph of Earth, cropped and resized to
  512 × 512. Source: [NASA Blue Marble history](https://science.nasa.gov/earth/earth-observatory/history-of-the-blue-marble/).
- `photo_moon.jpeg`: Galileo's PIA00405 lunar photograph, resized to 512 × 512.
  Source: [NASA Earth's Moon](https://science.nasa.gov/photojournal/earths-moon/).
- `video_earth_moon.mp4` and `video_moon_earth.mp4`: four-second H.264 videos
  containing those actual photographs, two seconds per photograph in opposite
  orders. These test temporal visual ordering; they do not represent natural
  scene motion. They have no sound, titles or subtitles.
- `nasa_eagle.wav` and `nasa_eagle.mp3`: the complete short
  [Apollo 11 recording provided by NASA](https://www.nasa.gov/historical-sounds/).
  The expected transcript comes from
  [NASA's published quotation](https://www.nasa.gov/missions/apollo/apollo-11/wide-awake-on-the-sea-of-tranquillity/).
  It is a source-derived expectation, not an offline speech-recognition result.

NASA is acknowledged as the source of the input material. The
[NASA media usage guidelines](https://www.nasa.gov/nasa-brand-center/images-and-media/)
permit factual educational/informational use subject to their terms; no NASA
endorsement is implied. Outputs from a tested model are that model's outputs.
The images contain no logos or identifiable people. Source files, original
URLs, source SHA256 values, credits and conversion commands are recorded in
`real_manifest.json`; original public bytes are retained under `sources/`.

Regenerate derived real media offline with:

```bash
python fixtures/media_input/generate_real_fixtures.py
```

The generator verifies the source hashes before resizing/transcoding. Runtime
fixture reads do not require Pillow, ffmpeg or network access. Regeneration of
the older synthetic fixtures does not overwrite `real_manifest.json`.

`remote_manifest.json` separately records an actual six-second public MP4 from
Zhipu's official documentation. Its bytes were downloaded, hashed and decoded;
the visual check identified three elephants moving in water. It is available
through `remote_video_fixture("zhipu_elephants")` as a fixed HTTPS URL and pinned
metadata. The original video is not redistributed. The live dispatcher must
recheck its content hash around model calls, because remote content can change.
