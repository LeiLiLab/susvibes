#!/bin/bash
set -e

echo "Installing Kimi CLI..."

# Ensure curl and build essentials are present if package manager is available
if command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y curl ca-certificates
elif command -v yum >/dev/null 2>&1; then
    yum install -y curl ca-certificates
fi

# Install uv in user home / root
if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# Create an isolated virtual environment for Kimi CLI to avoid conflicting with repository dependencies
echo "Setting up isolated Kimi CLI environment via uv..."
mkdir -p /root/.kimi-env
uv venv /root/.kimi-env

if /root/.kimi-env/bin/uv pip install kimi-cli; then
    echo "kimi-cli installed via uv pip"
elif uv tool install kimi-cli; then
    echo "kimi-cli installed via uv tool"
else
    pip install --no-cache-dir kimi-cli
fi

mkdir -p /root/.local/bin
if [ -f "/root/.kimi-env/bin/kimi" ]; then
    ln -sf /root/.kimi-env/bin/kimi /root/.local/bin/kimi
    ln -sf /root/.kimi-env/bin/kimi /usr/local/bin/kimi 2>/dev/null || true
fi

echo "Setting up environment..."
mkdir -p /root/.kimi
cat > /root/.kimi/.env << 'EOF'
export PATH="/root/.kimi-env/bin:/root/.local/bin:/root/.cargo/bin:$PATH"
export KIMI_MODEL="${KIMI_MODEL:-kimi-k2-turbo-preview}"
export MOONSHOT_API_KEY="${MOONSHOT_API_KEY:-}"
export KIMI_API_KEY="${KIMI_API_KEY:-$MOONSHOT_API_KEY}"
EOF

if ! grep -q "source /root/.kimi/.env" /root/.bashrc 2>/dev/null; then
    echo "source /root/.kimi/.env" >> /root/.bashrc
fi

echo "Testing installation..."
source /root/.kimi/.env

if command -v kimi >/dev/null 2>&1; then
    kimi --version || true
    echo "INSTALL_SUCCESS"
else
    echo "Installation failed: kimi executable not found" >&2
    exit 1
fi
