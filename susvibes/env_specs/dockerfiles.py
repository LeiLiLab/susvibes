DOCKERFILE_BASE_PY = r"""
FROM python:{version}
# Pass `{version}` as the full Docker Hub tag for the desired Python minor.
# Use non-slim (full) tags — we rely on the preinstalled build toolchain
# (gcc, g++, make, git, plus libssl/libffi/etc. -dev headers) for pip builds.
# Each minor is pinned to the newest Debian base its official image supports:
#   2.7  -> python:2.7-buster      (Debian 10; Python 2 EOL 2020, no newer base)
#   3.5  -> python:3.5-buster      (Debian 10; Python 3.5 EOL 2020, no newer base)
#   3.6  -> python:3.6-bullseye    (Debian 11; Python 3.6 EOL 2021, no newer base)
#   3.7  -> python:3.7-bookworm    (Debian 12)
#   3.8  -> python:3.8-bookworm    (Debian 12)
#   3.9  -> python:3.9-bookworm    (Debian 12)
#   3.10 -> python:3.10-bookworm   (Debian 12)
#   3.11 -> python:3.11-bookworm   (Debian 12)
#   3.12 -> python:3.12-bookworm   (Debian 12)

# EOL Debian releases (<bullseye, i.e. stretch/buster) have moved off
# deb.debian.org to archive.debian.org and their signing keys have expired.
# Redirect sources and relax verification only for those releases.
RUN . /etc/os-release && \
    if [ "$VERSION_ID" -lt 11 ]; then \
        sed -i 's|http://deb.debian.org|http://archive.debian.org|g; \
                s|http://security.debian.org|http://archive.debian.org|g; \
                /-updates/d; /-backports/d' /etc/apt/sources.list && \
        printf 'Acquire::Check-Valid-Until "false";\n\
Acquire::AllowInsecureRepositories "true";\n\
Acquire::AllowDowngradeToInsecureRepositories "true";\n\
APT::Get::AllowUnauthenticated "true";\n' > /etc/apt/apt.conf.d/99eol; \
    fi

# Suppress apt/dpkg interactivity so unattended builds don't hang on prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV APT_LISTCHANGES_FRONTEND=none
ENV TZ=Etc/UTC
RUN printf 'force-confdef\nforce-confold\n' > /etc/dpkg/dpkg.cfg.d/99force-conf

# Install common utilities and build dependencies, then clean up apt cache
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        git \
        build-essential && \
    rm -rf /var/lib/apt/lists/*

CMD ["python", "--version"]
"""

# dind_py = base_py + docker.io CLI. Build with `FROM base_py:{version}` so the
# two stay in lockstep automatically.
DOCKERFILE_DIND_PY = r"""
FROM base_py:{version}

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        docker.io && \
    rm -rf /var/lib/apt/lists/*
"""

DOCKERFILE_ENV_PY_TEMPLATE = r"""
FROM {base_image}

{system_installation_commands}

WORKDIR /project

COPY . .

{dependency_installation_commands}

CMD {test_running_commands}
"""

DOCKERFILE_INSTANCE_PY_TEMPLATE = r"""
FROM {base_image}

COPY . .

{dependency_installation_commands}

CMD {test_running_commands}
"""