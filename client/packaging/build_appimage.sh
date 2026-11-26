#!/usr/bin/env bash
# Kru для Linux одним файлом: AppImage з готової збірки PyInstaller (dist/Kru).
#   client/packaging/build_appimage.sh VERSION [DIST_DIR] [OUT_DIR]
# Потрібен appimagetool (APPIMAGETOOL=шлях або в PATH). Працює на будь-якому дистрибутиві без встановлення:
#   chmod +x Kru-*.AppImage && ./Kru-*.AppImage
set -euo pipefail
VERSION="${1:?версія, напр. 0.2.0}"
DIST="${2:-dist/Kru}"
OUT="${3:-.}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="${APPIMAGETOOL:-appimagetool}"
ARCH="${ARCH:-x86_64}"

[[ -x "${DIST}/Kru" ]] || { echo "Немає ${DIST}/Kru — спершу зберіть PyInstaller"; exit 1; }
APPDIR="$(mktemp -d)/Kru.AppDir"
mkdir -p "${APPDIR}/usr/lib" "${APPDIR}/usr/share/icons/hicolor"
cp -a "${DIST}" "${APPDIR}/usr/lib/kru"

cat > "${APPDIR}/AppRun" <<'RUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/kru/Kru" "$@"
RUN
chmod +x "${APPDIR}/AppRun"

sed "s|@EXEC@|Kru|" "${HERE}/kru-robot.desktop" | grep -v '^X-GNOME-Autostart' > "${APPDIR}/kru-robot.desktop"
echo "X-AppImage-Version=${VERSION}" >> "${APPDIR}/kru-robot.desktop"
# іконки — тим самим кодом, що й для встановлення (kru_robot.icon)
QT_QPA_PLATFORM=offscreen "${PYTHON:-python3}" -m kru_robot.icon "${APPDIR}/usr/share/icons"
cp "${APPDIR}/usr/share/icons/hicolor/256x256/apps/kru-robot.png" "${APPDIR}/kru-robot.png"
ln -s kru-robot.png "${APPDIR}/.DirIcon"

mkdir -p "${OUT}"
# --appimage-extract-and-run: у CI-контейнерах немає FUSE
ARCH="${ARCH}" "${TOOL}" --appimage-extract-and-run --no-appstream "${APPDIR}" "${OUT}/Kru-${VERSION}-${ARCH}.AppImage" \
  || ARCH="${ARCH}" "${TOOL}" --no-appstream "${APPDIR}" "${OUT}/Kru-${VERSION}-${ARCH}.AppImage"
echo "Готово: ${OUT}/Kru-${VERSION}-${ARCH}.AppImage"
