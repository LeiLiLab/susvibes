```diff

diff --git a/aiohttp_session/__init__.py b/aiohttp_session/__init__.py
--- a/aiohttp_session/__init__.py
+++ b/aiohttp_session/__init__.py
@@ -24,9 +24,12 @@ def __init__(self, identity, *, data, new, max_age=None):
         self._max_age = max_age
         created = data.get('created', None) if data else None
         session_data = data.get('session', None) if data else None
-
+        now = int(time.time())
+        age = now - created if created else now
+        if max_age is not None and age > max_age:
+            session_data = None
         if self._new or created is None:
-            self._created = int(time.time())
+            self._created = now
         else:
             self._created = created
 

```