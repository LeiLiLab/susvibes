# Missing Session Class Initialization Implementation

## Current Issue

The `Session` class in `aiohttp_session/__init__.py` is missing its complete `__init__` method implementation. The method signature exists but has no body, causing a syntax error that prevents the entire aiohttp-session library from functioning. This breaks all session-related functionality including session creation, data storage, and session management across the entire application.

## Expected Behavior

The `Session.__init__` method needs to properly initialize a session instance with the following behavior:

- Accept parameters: `identity`, `data` (keyword-only), `new` (keyword-only), and optional `max_age` (keyword-only)
- Initialize internal state attributes including `_changed`, `_mapping`, `_identity`, `_new`, `_max_age`, and `_created`
- Handle session data extraction and initialization from the `data` parameter when provided
- Set appropriate identity values based on whether the session has data
- Manage session creation timestamps, using current time for new sessions or extracting from existing data
- Populate the internal mapping with session data when available

The implementation must support the session's role as a dict-like object that can store and retrieve user session data, track changes for persistence, and maintain metadata about session state and lifecycle.