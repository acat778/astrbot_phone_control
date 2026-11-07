package fun.acat.uicapture;

import android.app.UiAutomation;
import android.graphics.Rect;
import android.os.Build;
import android.os.SystemClock;
import android.util.Xml;
import android.view.accessibility.AccessibilityNodeInfo;
import java.io.FileOutputStream;
import java.io.OutputStream;
import java.io.StringWriter;
import org.xmlpull.v1.XmlSerializer;

/** 通过 shell 无障碍连接读取当前节点；每次采集单独连接并完整关闭。 */
public final class UiCapture {
    private static int count;

    private static String text(CharSequence value) {
        if (value == null) return "";
        StringBuilder result = new StringBuilder();
        String raw = value.toString();
        for (int offset = 0; offset < raw.length();) {
            int cp = raw.codePointAt(offset);
            offset += Character.charCount(cp);
            if (cp == 9 || cp == 10 || cp == 13 || (cp >= 32 && cp <= 0xd7ff)
                    || (cp >= 0xe000 && cp <= 0xfffd) || (cp >= 0x10000 && cp <= 0x10ffff))
                result.appendCodePoint(cp);
        }
        return result.toString();
    }

    private static void attr(XmlSerializer xml, String key, Object value) throws Exception {
        xml.attribute("", key, String.valueOf(value));
    }

    private static void node(XmlSerializer xml, AccessibilityNodeInfo info, int depth) throws Exception {
        if (++count > 20000 || depth > 100) throw new IllegalStateException("节点树超过采集上限");
        Rect bounds = new Rect();
        info.getBoundsInScreen(bounds);
        xml.startTag("", "node");
        attr(xml, "text", text(info.getText()));
        attr(xml, "content-desc", text(info.getContentDescription()));
        attr(xml, "resource-id", text(info.getViewIdResourceName()));
        attr(xml, "class", text(info.getClassName()));
        attr(xml, "package", text(info.getPackageName()));
        attr(xml, "bounds", "[" + bounds.left + "," + bounds.top + "][" + bounds.right + "," + bounds.bottom + "]");
        attr(xml, "clickable", info.isClickable());
        attr(xml, "long-clickable", info.isLongClickable());
        attr(xml, "enabled", info.isEnabled());
        attr(xml, "focused", info.isFocused());
        attr(xml, "selected", info.isSelected());
        attr(xml, "scrollable", info.isScrollable());
        attr(xml, "checkable", info.isCheckable());
        attr(xml, "checked", info.isChecked());
        if (Build.VERSION.SDK_INT >= 26) {
            attr(xml, "hint", text(info.getHintText()));
            attr(xml, "showing-hint-text", info.isShowingHintText());
        }
        for (int i = 0; i < info.getChildCount(); i++) {
            AccessibilityNodeInfo child = info.getChild(i);
            if (child == null) continue;
            try {
                if (child.isVisibleToUser()) node(xml, child, depth + 1);
            } finally { child.recycle(); }
        }
        xml.endTag("", "node");
    }

    public static void main(String[] args) {
        try { capture(args); }
        catch (Exception error) {
            System.err.println("ERROR: " + error);
            System.exit(1);
        }
    }

    private static void capture(String[] args) throws Exception {
        if (args.length != 1) throw new IllegalArgumentException("需指定输出路径");
        Class<?> wrapperClass = Class.forName("com.android.uiautomator.core.UiAutomationShellWrapper");
        Object wrapper = wrapperClass.getConstructor().newInstance();
        wrapperClass.getMethod("connect").invoke(wrapper);
        try {
            UiAutomation automation = (UiAutomation) wrapperClass.getMethod("getUiAutomation").invoke(wrapper);
            wrapperClass.getMethod("setCompressedLayoutHierarchy", boolean.class).invoke(wrapper, false);
            // 连接建立后等待根窗口可读，期限内不要求页面停止产生事件。
            long deadline = SystemClock.uptimeMillis() + 2000;
            AccessibilityNodeInfo root;
            do {
                root = automation.getRootInActiveWindow();
                if (root != null) break;
                SystemClock.sleep(50);
            } while (SystemClock.uptimeMillis() < deadline);
            if (root == null) throw new IllegalStateException("当前窗口没有可读取的根节点");
            StringWriter buffer = new StringWriter();
            XmlSerializer xml = Xml.newSerializer();
            xml.setOutput(buffer);
            xml.startDocument("UTF-8", true);
            xml.startTag("", "hierarchy");
            attr(xml, "rotation", 0);
            try { node(xml, root, 0); } finally { root.recycle(); }
            xml.endTag("", "hierarchy");
            xml.endDocument();
            byte[] bytes = buffer.toString().getBytes("UTF-8");
            if ("/dev/tty".equals(args[0])) {
                System.out.write(bytes); System.out.flush();
            } else {
                try (OutputStream output = new FileOutputStream(args[0])) { output.write(bytes); }
                System.out.println("UI hierarchy dumped to: " + args[0]);
            }
        } finally { wrapperClass.getMethod("disconnect").invoke(wrapper); }
    }
}
