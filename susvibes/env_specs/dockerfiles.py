DOCKERFILE_BASE_PY = r"""
FROM python:{version}-slim

# Prevent interactive prompts during package install and set timezone
ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

# Install common utilities and build dependencies, then clean up apt cache
RUN apt-get update
    apt-get install -y --no-install-recommends \
        docker.io && \
        curl \
        git \
        build-essential && \
    rm -rf /var/lib/apt/lists/*

CMD ["python", "--version"]
"""
# Note: the live `base_py:{3.7..3.12}` images on Docker Hub (songwen6968/base_py)
# are NOT a clean rebuild from this Dockerfile — they were extended in-place via
# three successive `docker build -t base_py:$v -t songwen6968/base_py:$v` layers
# on top of the originally pushed image (FROM the previous tag each time):
#   1. ENV DEBIAN_FRONTEND=noninteractive
#   2. ENV APT_LISTCHANGES_FRONTEND=none, TZ=Etc/UTC
#   3. RUN printf 'force-confdef\nforce-confold\n' > /etc/dpkg/dpkg.cfg.d/99force-conf
# All three suppress apt/dpkg interactivity. Rebuilding from this file will drop
# those layers, so prefer `docker pull songwen6968/base_py:$v` to stay aligned.

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