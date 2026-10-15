# Пісочниця для run_shell / run_python агента Kru: Python і типові бібліотеки та утиліти.
# Збирається командою ../sandbox.sh build. Кожна команда агента — одноразовий контейнер, який бачить лише
# робочу теку користувача (/workspace); бібліотеки з pip install лягають у /workspace/.kru-py.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils findutils grep sed gawk file less \
        curl wget ca-certificates git jq \
        zip unzip xz-utils p7zip-full \
        sqlite3 poppler-utils ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        numpy pandas scipy sympy matplotlib pillow \
        requests beautifulsoup4 lxml \
        openpyxl xlsxwriter python-docx pypdf tabulate pyyaml

WORKDIR /workspace
CMD ["bash"]
