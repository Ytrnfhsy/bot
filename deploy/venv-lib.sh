# Спільне для інсталяторів (source-иться): створити venv і поставити туди пакет.
# Якщо pypi.org недоступний, а venv уже є — оновлюємо лише сам код (залежності лишаються наявні).

# kru_venv_install VENV SRC_DIR PKG ENTRY MODULE:FUNC
#   PKG — Python-пакет у SRC_DIR (напр. kru_agent), ENTRY — команда в venv/bin (kru)
kru_venv_install() {
  local venv="$1" src="$2" pkg="$3" entry="$4" func="$5"
  [[ -x "${venv}/bin/python" ]] || python3 -m venv "${venv}"
  local pip=("${venv}/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir)
  if "${pip[@]}" --upgrade pip >/dev/null 2>&1 && "${pip[@]}" "${src}" \
     && "${pip[@]}" --force-reinstall --no-deps "${src}"; then
    return 0
  fi
  [[ -x "${venv}/bin/${entry}" ]] || { echo "Не вдалося встановити ${pkg}: немає доступу до pypi.org"; return 1; }
  echo "⚠ pip не зміг завантажити пакети — оновлюю лише код (залежності лишаються попередні)"
  local site
  site="$("${venv}/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  rm -rf "${site:?}/${pkg}"
  cp -r "${src}/${pkg}" "${site}/${pkg}"
}
