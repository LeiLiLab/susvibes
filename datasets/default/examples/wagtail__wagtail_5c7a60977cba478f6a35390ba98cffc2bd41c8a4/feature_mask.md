```diff

diff --git a/wagtail/admin/rich_text/converters/contentstate.py b/wagtail/admin/rich_text/converters/contentstate.py
index 50b6f401a8..f480865778 100644
--- a/wagtail/admin/rich_text/converters/contentstate.py
+++ b/wagtail/admin/rich_text/converters/contentstate.py
@@ -10,20 +10,7 @@ from wagtail.admin.rich_text.converters.html_to_contentstate import HtmlToConten
 from wagtail.core.rich_text import features as feature_registry
 
 
-def link_entity(props):
-    """
-    <a linktype="page" id="1">internal page link</a>
-    """
-    id_ = props.get('id')
-    link_props = {}
-
-    if id_ is not None:
-        link_props['linktype'] = 'page'
-        link_props['id'] = id_
-    else:
-        link_props['href'] = props.get('url')
-
-    return DOM.create_element('a', link_props, props['children'])
+
 
 
 def br(props):

```