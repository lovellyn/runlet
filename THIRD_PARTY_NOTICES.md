# Third-party software

The Apache License 2.0 in this repository covers Runlet's own source code.
Dependencies and software distributed in the container retain their respective
licenses and notices.

The image contains Ubuntu packages, Node.js/npm, Python, the Python MCP SDK,
Pillow, Tini, FFmpeg, GitHub CLI, and other packages selected by the Dockerfile and
requirements.txt. This is not an exhaustive package inventory.

Ubuntu package copyright and license information is normally installed under
/usr/share/doc/<package>/copyright. Python distributions include metadata and
license files under /opt/runlet/lib/python3.12/site-packages.
Node's complete upstream license is retained at /usr/local/share/doc/node/LICENSE,
and npm's license is at /usr/local/lib/node_modules/npm/LICENSE. This document
is also included at /app/THIRD_PARTY_NOTICES.md.
GitHub CLI's upstream MIT license is retained at /usr/local/share/doc/gh/LICENSE.
The release workflow requests a BuildKit SBOM and provenance attestation for
each published container image.

FFmpeg's applicable licenses depend on its enabled libraries and build options.
The Ubuntu build includes GPL components. Inspect the actual image with:

~~~bash
ffmpeg -L
ffmpeg -buildconf
~~~

Ubuntu source packages, including FFmpeg and its patches, are available from
[Ubuntu Launchpad](https://launchpad.net/ubuntu/+source/ffmpeg). Use the installed
package versions and their copyright files to identify matching component sources.

Retain relevant notices and satisfy the licenses of bundled components when
redistributing an image. The project's Apache License 2.0 does not relicense them.
