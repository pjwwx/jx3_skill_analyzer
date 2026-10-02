# Third-party notices

## PakV4-Extract

- Upstream project: https://github.com/jx3pak/PakV4-Extract
- Upstream description: JX3 Pak/PakV2/PakV3/PakV4/PakV5 extraction source
- Use in this project: the PakV4 interoperability implementation in `native/jx3_pak_extract.cpp` is based on this upstream source, including the game DLL interface declarations, PakV4 initialization sequence, resource lookup and resource reading flow
- Changes in this project: rewritten command-line contract, explicit game/output paths, current-directory independence, diagnostics, batch output handling and GUI-managed temporary execution
- Generated binary: `vendor/JX3PakBridge.exe`, compiled locally from the source shipped in this project and excluded from Git

The upstream repository did not include a separate license file when this notice was written. This declaration identifies the source and scope of use; it does not claim that the upstream work was authored by this project. The adapted helper remains available as source so its behavior can be audited and rebuilt.

## unluac-rs 1.4.4

- Project: https://github.com/x3zvawq/unluac-rs
- Copyright: unluac-rs contributors
- License: MIT
- Generated file: `vendor/unluac.exe`, built locally from the pinned upstream `v1.4.4` source by `build-unluac.ps1` and excluded from Git

This source repository does not distribute the executable. Source-built binaries can differ from upstream release binaries because of compiler and build-environment differences.

The complete MIT license text supplied by the project is included as `vendor/LICENSE-unluac-rs.txt`.

## PySide6 Essentials / Qt 6

- Project: https://doc.qt.io/qtforpython-6/
- Copyright: The Qt Company Ltd. and Qt contributors
- License: LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only, with commercial licensing also available
- Use in this project: desktop user interface and dynamically loaded Qt libraries

This project uses the LGPL-3.0-only option. The executable does not modify Qt. Qt/PySide source and license information are available from https://code.qt.io/cgit/pyside/pyside-setup.git/ and https://www.qt.io/licensing/open-source-lgpl-obligations.

## openpyxl

- Project: https://openpyxl.readthedocs.io/
- Copyright: openpyxl contributors
- License: MIT
- Use in this project: local Excel workbook export
- License text: https://foss.heptapod.net/openpyxl/openpyxl/-/blob/3.1.5/LICENCE.rst

Its XML dependency `et-xmlfile` is also MIT licensed: https://foss.heptapod.net/openpyxl/et_xmlfile/-/blob/2.0.0/LICENCE.rst
