# Missing Password Verification Function in Django Auth Hashers

## Current Issue

The Django authentication system's password hashing module (`django.contrib.auth.hashers`) is missing a critical internal function `verify_password()` that is required by both the `check_password()` and `acheck_password()` functions. This causes both functions to fail with a `NameError` when attempting to verify passwords, breaking password authentication throughout the Django application.

The missing function is called by:
- `check_password(password, encoded, setter=None, preferred="default")` at line 75
- `acheck_password(password, encoded, setter=None, preferred="default")` at line 83

Both functions expect `verify_password()` to return a tuple of two booleans: `(is_correct, must_update)`.

## Expected Behavior

The `verify_password()` function should be implemented to:

1. Accept parameters: `password`, `encoded`, and `preferred="default"`
2. Return a tuple of two booleans:
   - First boolean: whether the raw password matches the encoded digest
   - Second boolean: whether the password needs to be regenerated/updated
3. Handle edge cases like `None` passwords and unusable password encodings
4. Support password hasher algorithm changes and updates
5. Implement timing attack protection through runtime hardening when appropriate

The function must integrate seamlessly with the existing password hashing infrastructure, including hasher identification, algorithm preferences, and the `must_update()` and `harden_runtime()` methods of password hashers.

## Implementation Objective

Implement the missing `verify_password()` function in `django/contrib/auth/hashers.py` to restore full functionality to the password verification system, ensuring that both synchronous and asynchronous password checking work correctly while maintaining all existing behavior and interfaces.