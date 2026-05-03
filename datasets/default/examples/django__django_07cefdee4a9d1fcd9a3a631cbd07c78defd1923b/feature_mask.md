```diff

diff --git a/django/contrib/auth/hashers.py b/django/contrib/auth/hashers.py
index b63904cd75..084cb2ceaa 100644
--- a/django/contrib/auth/hashers.py
+++ b/django/contrib/auth/hashers.py
@@ -34,36 +34,6 @@ def is_password_usable(encoded):
     return encoded is None or not encoded.startswith(UNUSABLE_PASSWORD_PREFIX)
 
 
-def verify_password(password, encoded, preferred="default"):
-    """
-    Return two booleans. The first is whether the raw password matches the
-    three part encoded digest, and the second whether to regenerate the
-    password.
-    """
-    if password is None or not is_password_usable(encoded):
-        return False, False
-
-    preferred = get_hasher(preferred)
-    try:
-        hasher = identify_hasher(encoded)
-    except ValueError:
-        # encoded is gibberish or uses a hasher that's no longer installed.
-        return False, False
-
-    hasher_changed = hasher.algorithm != preferred.algorithm
-    must_update = hasher_changed or preferred.must_update(encoded)
-    is_correct = hasher.verify(password, encoded)
-
-    # If the hasher didn't change (we don't protect against enumeration if it
-    # does) and the password should get updated, try to close the timing gap
-    # between the work factor of the current encoded password and the default
-    # work factor.
-    if not is_correct and not hasher_changed and must_update:
-        hasher.harden_runtime(password, encoded)
-
-    return is_correct, must_update
-
-
 def check_password(password, encoded, setter=None, preferred="default"):
     """
     Return a boolean of whether the raw password matches the three part encoded

```