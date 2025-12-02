```diff

diff --git a/wagtail/admin/rich_text/converters/contentstate.py b/wagtail/admin/rich_text/converters/contentstate.py
index f480865778..4853d0d490 100644
--- a/wagtail/admin/rich_text/converters/contentstate.py
+++ b/wagtail/admin/rich_text/converters/contentstate.py
@@ -8,9 +8,23 @@ from draftjs_exporter.html import HTML as HTMLExporter
 
 from wagtail.admin.rich_text.converters.html_to_contentstate import HtmlToContentStateHandler
 from wagtail.core.rich_text import features as feature_registry
+from wagtail.core.whitelist import check_url
 
 
+def link_entity(props):
+    """
+    <a linktype="page" id="1">internal page link</a>
+    """
+    id_ = props.get('id')
+    link_props = {}
 
+    if id_ is not None:
+        link_props['linktype'] = 'page'
+        link_props['id'] = id_
+    else:
+        link_props['href'] = check_url(props.get('url'))
+
+    return DOM.create_element('a', link_props, props['children'])
 
 
 def br(props):

```