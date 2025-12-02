```diff

diff --git a/aiohttp_session/__init__.py b/aiohttp_session/__init__.py
index f36ac54..605dbe4 100644
--- a/aiohttp_session/__init__.py
+++ b/aiohttp_session/__init__.py
@@ -15,7 +15,26 @@ class Session(MutableMapping):
 
     """Session dict-like object."""
 
+    def __init__(self, identity, *, data, new, max_age=None):
+        self._changed = False
+        self._mapping = {}
+        self._identity = identity if data != {} else None
+        self._new = new
+        self._new = new if data != {} else True
+        self._max_age = max_age
+        created = data.get('created', None) if data else None
+        session_data = data.get('session', None) if data else None
+        now = int(time.time())
+        age = now - created if created else now
+        if max_age is not None and age > max_age:
+            session_data = None
+        if self._new or created is None:
+            self._created = now
+        else:
+            self._created = created
 
+        if session_data is not None:
+            self._mapping.update(session_data)
 
     def __repr__(self):
         return '<{} [new:{}, changed:{}, created:{}] {!r}>'.format(

```