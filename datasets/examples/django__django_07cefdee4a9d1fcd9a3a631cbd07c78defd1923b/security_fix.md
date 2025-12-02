```diff

diff --git a/django/contrib/auth/hashers.py b/django/contrib/auth/hashers.py
--- a/django/contrib/auth/hashers.py
+++ b/django/contrib/auth/hashers.py
@@ -40,14 +40,20 @@ def verify_password(password, encoded, preferred="default"):
     three part encoded digest, and the second whether to regenerate the
     password.
     """
-    if password is None or not is_password_usable(encoded):
-        return False, False
+    fake_runtime = password is None or not is_password_usable(encoded)
 
     preferred = get_hasher(preferred)
     try:
         hasher = identify_hasher(encoded)
     except ValueError:
         # encoded is gibberish or uses a hasher that's no longer installed.
+        fake_runtime = True
+
+    if fake_runtime:
+        # Run the default password hasher once to reduce the timing difference
+        # between an existing user with an unusable password and a nonexistent
+        # user or missing hasher (similar to #20760).
+        make_password(get_random_string(UNUSABLE_PASSWORD_SUFFIX_LENGTH))
         return False, False
 
     hasher_changed = hasher.algorithm != preferred.algorithm

```