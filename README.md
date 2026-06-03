# SlopBro

Some of the Python was written by an LLM. (You think I wanted to write a
Python 2.7/3.x-compatible WebSocket client?) The rest is good, old-fashioned
**human** slop. It's the spirit of the times, you know?

## Overview

SlopBro is a proof-of-concept exploit for the jsserver vulnerability in
LG TVs.

I tried to make it compatible with Python 2.7 and 3.x with no dependencies
outside the standard library so that it could be run on all versions of
webOS TV. Unfortunately, the Python 2.7 environment on webOS 6 (and
presumably older) is missing the HTTP server stuff, so it was kind of a
waste of time. Oh well.

I've only minimally tested it on webOS 6 (remember: slop!), but it
should probably work on at least some other versions.

I also tested some parts on webOS 10 (25), and other people have used it
successfully on webOS 5 and 7. I believe it should work on webOS 11 (26).

### How it works

`slopbro.py` is the main script. It performs the following steps:

1. Starts an HTTP server to serve the exploit page and payloads.
2. Opens an SSAP connection to the TV.
3. Launches a WAM app pointing at the exploit page `index.html`.

Pages running in the context of certain WAM apps can make privileged
Luna requests. When loaded on the TV, `index.html` does the following:

1. Displays status and debug info on the TV screen.
2. Downloads files for a fake `com.webos.service.jsserver` package from the
   HTTP server.
3. Runs the package using the jsserver vulnerability.

The entry point of the fake `com.webos.service.jsserver` package is `main.js`,
which is executed with root privileges. It is responsible for launching
`autoroot.sh`, which installs Homebrew Channel and enables persistence.

## Running

Run the script with Python, passing the IP address of your TV:

```bash
python slopbro.py [--debug] [--local-ip <LOCAL IP>] [--asset-source <auto|dir|embedded>] <TV IP ADDRESS>
```

*NOTE: On webOS 7+, you may have to use `python3` instead of `python`.*

Accept the pairing prompt on the target TV. (The credentials will be saved in
a `.key` file for future use.)

### Options

The `--debug` option enables extra output on the TV screen as well as in
`autoroot.log`.

The `--local-ip` option allows you to specify the local IP address manually,
which can be useful if the script guesses the wrong IP address.

The `--asset-source` option allows you to specify where the script should look
for assets (`auto`, `dir`, `embedded`).

## Packaging

In addition to serving files from the `wwwroot` directory, SlopBro can be
distributed as a single file with embedded assets.

### Building a single-file package

Generate a standalone file with:

```bash
python tools/package_single_file.py --out dist/slopbro_packed.py
```

Then run it directly (no `wwwroot` required):

```bash
python dist/slopbro_packed.py 192.168.1.50
```

You can also explicitly specify where it should look for assets
(`embedded`, `dir`):

```bash
python dist/slopbro_packed.py --asset-source embedded 192.168.1.50
```

By default (`auto` mode), embedded assets are preferred over files if both
are present.

## Troubleshooting

- `slopbro.py` guesses what the local IP address is and might get it wrong.
  If you don't see any connections back to the HTTP server, try manually
  specifying the IP address with `--local-ip`.

- Make sure there are no weird network issues between your TV and wherever
  you're running SlopBro. Remember that connections need to work in both
  directions.

## Credits

IDK, Claude Sonnet 4.6?

(`dangbei-overlay` from [dangbro](https://github.com/azoffshowy/dangbro);
jsserver vulnerability first publicly disclosed in
[jsbro-autoroot](https://github.com/raws0kil/jsbro-autoroot).)

## License

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License along
with this program. If not, see <https://www.gnu.org/licenses/>.

See `COPYING` for details.
