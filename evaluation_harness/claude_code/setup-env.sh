#!/bin/bash

echo "Installing Real Claude CLI..."

apt-get update
apt-get install -y curl

curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh | bash

source "$HOME/.nvm/nvm.sh"

nvm install 22
npm -v

npm install -g @anthropic-ai/claude-code@1.0.128

pip3 install "harness[sdk]"

echo "Setting up environment..."
cat > /root/.claude_env << 'EOF'
export ANTHROPIC_MODEL="claude-sonnet-4-20250514"
export FORCE_AUTO_BACKGROUND_TASKS="1"
export ENABLE_BACKGROUND_TASKS="1"
EOF

echo "source /root/.claude_env" >> /root/.bashrc

echo "Testing installation..."
source /root/.claude_env
source "$HOME/.nvm/nvm.sh"

if command -v claude >/dev/null 2>&1; then
  claude --version
  echo "INSTALL_SUCCESS"
else
  echo "Installation failed" >&2
  exit 1
fi