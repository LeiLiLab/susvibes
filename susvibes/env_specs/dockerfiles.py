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