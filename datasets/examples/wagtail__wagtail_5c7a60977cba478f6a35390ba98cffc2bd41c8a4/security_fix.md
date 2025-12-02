```diff

diff --git a/wagtail/admin/rich_text/converters/contentstate.py b/wagtail/admin/rich_text/converters/contentstate.py
--- a/wagtail/admin/rich_text/converters/contentstate.py
+++ b/wagtail/admin/rich_text/converters/contentstate.py
@@ -8,6 +8,7 @@
 
 from wagtail.admin.rich_text.converters.html_to_contentstate import HtmlToContentStateHandler
 from wagtail.core.rich_text import features as feature_registry
+from wagtail.core.whitelist import check_url
 
 
 def link_entity(props):
@@ -21,7 +22,7 @@ def link_entity(props):
         link_props['linktype'] = 'page'
         link_props['id'] = id_
     else:
-        link_props['href'] = props.get('url')
+        link_props['href'] = check_url(props.get('url'))
 
     return DOM.create_element('a', link_props, props['children'])
 

```