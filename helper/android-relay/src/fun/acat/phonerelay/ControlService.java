package fun.acat.phonerelay;

import android.app.*;
import android.content.*;
import android.os.*;
import android.util.Base64;
import java.io.*;
import java.net.*;
import java.security.*;
import java.security.cert.*;
import java.util.*;
import java.util.concurrent.*;
import javax.net.ssl.*;
import org.json.JSONObject;

/** App 主动建立固定证书 TLS 连接，本机 ADB 执行请求。 */
public final class ControlService extends Service {
    public static volatile String status = "App 控制已停止";
    public static volatile boolean connected;
    public static volatile JSONObject updateInfo;
    private volatile boolean running;
    private static volatile ControlService activeService;
    private final Object cleanupLock=new Object();
    private static CompletableFuture<Void> lastCleanup=CompletableFuture.completedFuture(null);
    private CompletableFuture<Void> predecessorCleanup=CompletableFuture.completedFuture(null);
    private volatile SSLSocket socket;
    private volatile LocalAdb adb;
    private volatile PhoneTraffic traffic;
    private volatile LocalStreams streams;
    private volatile PhoneRecorder recorder;
    private volatile JSONObject connectionCode;
    private volatile DataOutputStream output;
    private volatile String session;
    private ExecutorService network;
    private ExecutorService work;
    private ScheduledExecutorService timers;
    private final ConcurrentHashMap<String, FutureTask<Void>> jobs = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, CompletableFuture<JSONObject>> downloads = new ConcurrentHashMap<>();
    private long lastRequestSequence;

    private static void requireLocal(String host) throws Exception {
        InetAddress address = InetAddress.getByName(host);
        if (!address.isLoopbackAddress() && NetworkInterface.getByInetAddress(address) == null)
            throw new IOException("ADB 地址必须属于本机");
    }
    static boolean isActive() {
        synchronized(ControlService.class) { return activeService!=null && activeService.running; }
    }
    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        SharedPreferences prefs=getSharedPreferences("control",0);
        if (intent!=null && "stop".equals(intent.getAction())) { prefs.edit().putBoolean("control_wanted",false).apply();stopSelf();return START_NOT_STICKY; }
        if (intent==null && !(prefs.getBoolean("restore_enabled",false) && prefs.getBoolean("control_wanted",false))) {stopSelf();return START_NOT_STICKY;}
        String action = intent==null ? "restore" : intent.getAction();
        if ("policy".equals(action)) {
            if(!running) { stopSelf(startId);return START_NOT_STICKY; }
            return prefs.getBoolean("restore_enabled",false)?START_STICKY:START_NOT_STICKY;
        }
        if ("check".equals(action) || "install".equals(action)) {
            if (work == null || socket == null) {
                status = "请先连接插件";
                if(!running)stopSelf(startId);
                return running && prefs.getBoolean("restore_enabled",false)?START_STICKY:START_NOT_STICKY;
            }
            work.execute(() -> {
                try {
                    if (!jobs.isEmpty()) throw new IOException("请先停止手机任务再更新");
                    if ("check".equals(action)) {
                        updateInfo = requestUpdate(-1).getJSONObject("info");
                        long installed = UpdateInstaller.version(getPackageManager().getPackageInfo(getPackageName(), 0));
                        status = updateInfo.getInt("versionCode") > installed
                            ? "插件提供版本 " + updateInfo.getString("versionName") + "；下载后点击确认系统安装"
                            : "当前已是插件提供的版本 " + updateInfo.getString("versionName");
                    } else installUpdate();
                } catch (Exception e) { status = "更新失败：" + e.getMessage(); }
            });
            return prefs.getBoolean("restore_enabled",false)?START_STICKY:START_NOT_STICKY;
        }
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel("control", "手机 App 控制", NotificationManager.IMPORTANCE_LOW));
        PendingIntent stop = PendingIntent.getService(this, 31, new Intent(this, ControlService.class).setAction("stop"), PendingIntent.FLAG_IMMUTABLE);
        startForeground(31, new Notification.Builder(this, "control").setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentTitle("Phone ADB Relay").setContentText("本机 ADB 控制已启用")
            .addAction(new Notification.Action.Builder(null, "停止", stop).build()).build());
        prefs.edit().putBoolean("control_wanted",true).remove("recovery_error").apply();
        if (running) return prefs.getBoolean("restore_enabled",false)?START_STICKY:START_NOT_STICKY;
        synchronized(ControlService.class) {predecessorCleanup=lastCleanup;running=true;activeService=this;}
        network = Executors.newSingleThreadExecutor();
        work = Executors.newFixedThreadPool(2);
        timers = Executors.newScheduledThreadPool(2);
        network.execute(this::connectLoop);
        return prefs.getBoolean("restore_enabled",false)?START_STICKY:START_NOT_STICKY;
    }
    private void connectLoop() {
        android.content.SharedPreferences prefs = getSharedPreferences("control", 0);
        int retrySeconds=3;
        while (running) {
            String stage = "等待前次控制服务收尾";
            try {
                predecessorCleanup.get();
                if(!running)break;
                stage="读取本机 ADB 身份";
                String host = prefs.getString("adb_host", "127.0.0.1");
                requireLocal(host);
                adb = new LocalAdb(this);
                stage = "连接本机 ADB";
                int endpointPort=prefs.getInt("adb_port",5555);
                try {
                    if(!adb.connect(host,endpointPort))throw new IOException("本机ADB未授权");
                } catch(Exception original) {
                    if(!prefs.getBoolean("discover_port",false))throw original;
                    status="本机ADB连接失败，正在发现本机无线连接端口…";
                    WirelessAdbDiscovery.Endpoint endpoint=WirelessAdbDiscovery.find(this);
                    adb.close();adb=new LocalAdb(this);
                    if(!adb.connect(endpoint.host,endpoint.port))throw new IOException("发现了连接端口，但本机ADB尚未授权；请到无线配对页授权");
                    host=endpoint.host;endpointPort=endpoint.port;
                    prefs.edit().putString("adb_host",host).putInt("adb_port",endpointPort).apply();
                }
                JSONObject capabilities=LocalAdb.bounded("读取本机权限",5000,()->adb.capabilities(prefs.getString("adb_host","127.0.0.1"),prefs.getInt("adb_port",5555)));
                JSONObject code = new JSONObject(new String(Base64.decode(prefs.getString("code", ""), Base64.URL_SAFE), "UTF-8"));
                if (code.getInt("v") != 1) throw new IOException("配对码版本不支持");
                connectionCode = code;
                stage = "连接插件 TLS";
                SSLSocket active = connectPlugin(code); socket = active;
                output = new DataOutputStream(active.getOutputStream());
                DataInputStream input = new DataInputStream(active.getInputStream());
                String device = prefs.getString("device_id", "");
                if (device.isEmpty()) { device = UUID.randomUUID().toString(); prefs.edit().putString("device_id", device).apply(); }
                stage = "认证插件";
                send(capabilities.put("relay_host",active.getInetAddress().getHostAddress()).put("relay_port",active.getPort()).put("v", 1).put("token", code.getString("token")).put("device_id", device)
                    .put("features", new org.json.JSONArray(new String[]{"screen_frames", "traffic_proxy", "video_standby", "binary_streams", "media_recording", "frame_stream", "frame_options", "stream_progress", "frame_ack", "stream_credit"})).put("version", getPackageManager().getPackageInfo(getPackageName(), 0).versionName));
                JSONObject ready = read(input);
                if (!"ready".equals(ready.optString("type"))) throw new IOException("插件配对被拒绝");
                session = ready.getString("session"); lastRequestSequence = 0;
                traffic = new PhoneTraffic(this, adb);
                recorder = new PhoneRecorder(this, adb);
                final String dataSession=session;
                streams = new LocalStreams(adb, id -> dataConnection(id), id -> recorder.artifact(id),(id,count)-> {
                    if(!dataSession.equals(session) || socket!=active)throw new IOException("数据会话已结束");
                    try {send(new JSONObject().put("type","stream_progress").put("session",dataSession).put("stream_id",id).put("received_bytes",count));}
                    catch(Exception e) {throw new IOException("数据接收进度上报失败",e);}
                });
                synchronized(ControlService.class) {
                    if(!running || activeService!=this)throw new IOException("控制服务已停止");
                    connected=true;
                    status="App 控制已连接；ADB UID="+capabilities.getInt("adb_uid")+"；"+host+":"+endpointPort;
                }
                stage="控制会话";retrySeconds=3;
                ScheduledFuture<?> heartbeat = timers.scheduleAtFixedRate(() -> {
                    try { send(new JSONObject().put("type", "ping")); }
                    catch (Exception e) { closeSocket(); }
                }, 10, 10, TimeUnit.SECONDS);
                ScheduledFuture<?> uploads = timers.scheduleAtFixedRate(() -> {
                    try {
                        PhoneTraffic capture = traffic;
                        JSONObject event = capture == null ? null : capture.drain();
                        if (event != null) { event.put("session", session); send(event); }
                    } catch (Exception e) { closeSocket(); }
                }, 1, 1, TimeUnit.SECONDS);
                try { while (running && socket == active) handle(read(input)); }
                finally { heartbeat.cancel(false); uploads.cancel(false); }
            } catch (Exception e) {
                if (running) status = stage + "失败：" + (e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage()) + "；等待重新连接";
            } finally { cleanupConnection(); }
            if (running) try { Thread.sleep(retrySeconds*1000L);retrySeconds=Math.min(30,retrySeconds*2); } catch (InterruptedException e) { Thread.currentThread().interrupt(); break; }
        }
    }
    private static SSLSocket connectPlugin(JSONObject code) throws Exception {
        final String pin = code.getString("pin");
        if (!pin.matches("[0-9a-f]{64}")) throw new IOException("证书指纹无效");
        TrustManager[] trust = new TrustManager[]{new X509TrustManager() {
            public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            public void checkClientTrusted(X509Certificate[] chain, String auth) throws CertificateException { throw new CertificateException("仅连接服务端"); }
            public void checkServerTrusted(X509Certificate[] chain, String auth) throws CertificateException {
                try {
                    chain[0].checkValidity();
                    String actual = hex(MessageDigest.getInstance("SHA-256").digest(chain[0].getEncoded()));
                    if (!MessageDigest.isEqual(actual.getBytes("US-ASCII"), pin.getBytes("US-ASCII"))) throw new CertificateException("插件证书指纹不匹配");
                } catch (CertificateException e) { throw e; }
                catch (Exception e) { throw new CertificateException(e); }
            }
        }};
        SSLContext tls = SSLContext.getInstance("TLS"); tls.init(null, trust, new SecureRandom());
        SSLSocket active = (SSLSocket) tls.getSocketFactory().createSocket();
        try {
            active.connect(new InetSocketAddress(code.getString("host"), code.getInt("port")), 10000);
            active.setSoTimeout(45000); active.setTcpNoDelay(true); active.startHandshake();
            return active;
        } catch (Exception e) { active.close(); throw e; }
    }
    private Socket proxyConnection(String captureId) throws Exception {
        JSONObject code = connectionCode; String currentSession = session;
        if (!connected || code == null || currentSession == null) throw new IOException("插件连接已结束");
        SSLSocket peer = connectPlugin(code);
        try {
            JSONObject hello = new JSONObject().put("v", 1).put("type", "proxy").put("token", code.getString("token"))
                .put("device_id", getSharedPreferences("control", 0).getString("device_id", ""))
                .put("session", currentSession).put("capture_id", captureId);
            byte[] bytes = hello.toString().getBytes("UTF-8");
            DataOutputStream out = new DataOutputStream(peer.getOutputStream()); out.writeInt(bytes.length); out.write(bytes); out.flush();
            if (!"proxy_ready".equals(read(new DataInputStream(peer.getInputStream())).optString("type"))) throw new IOException("抓包隧道授权失败");
            return peer;
        } catch (Exception e) { peer.close(); throw e; }
    }
    private Socket dataConnection(String id) throws Exception {
        JSONObject code = connectionCode; String current = session;
        if (!connected || code == null || current == null) throw new IOException("插件连接已结束");
        SSLSocket peer = connectPlugin(code);
        try {
            JSONObject hello = new JSONObject().put("v", 1).put("type", "stream").put("token", code.getString("token"))
                .put("device_id", getSharedPreferences("control", 0).getString("device_id", ""))
                .put("session", current).put("stream_id", id);
            byte[] bytes = hello.toString().getBytes("UTF-8");
            DataOutputStream out = new DataOutputStream(peer.getOutputStream()); out.writeInt(bytes.length); out.write(bytes); out.flush();
            if (!"stream_ready".equals(read(new DataInputStream(peer.getInputStream())).optString("type"))) throw new IOException("数据通道授权失败");
            return peer;
        } catch (Exception e) { peer.close(); throw e; }
    }
    private void handle(JSONObject message) throws Exception {
        String type = message.optString("type"), id = message.optString("id");
        if ("pong".equals(type)) return;
        if ("update".equals(type)) {
            CompletableFuture<JSONObject> future = downloads.get(id);
            if (future != null) future.complete(message);
            return;
        }
        if (!session.equals(message.optString("session"))) throw new IOException("会话已失效");
        if("stream_received".equals(type)) {
            Object count=message.opt("received_bytes");
            if(!(count instanceof Integer || count instanceof Long))throw new IOException("数据接收进度无效");
            streams.received(message.getString("stream_id"),((Number)count).longValue());return;
        }
        if ("cancel".equals(type)) { cancel(id); return; }
        long requestSequence = message.getLong("seq");
        if (!"call".equals(type) || !id.matches("[0-9a-f]{32}") || requestSequence <= lastRequestSequence)
            throw new IOException("重复或无效请求");
        lastRequestSequence = requestSequence;
        long ttl = message.getLong("ttl_ms");
        if (ttl <= 0 || ttl > 300000 || jobs.size() >= 8) throw new IOException("请求期限或并发无效");
        final long deadline = SystemClock.elapsedRealtime() + ttl;
        final String requestSession = session;
        final LocalAdb local = adb;
        final PhoneTraffic currentTraffic=traffic;
        final PhoneRecorder currentRecorder=recorder;
        final LocalStreams currentStreams=streams;
        FutureTask<Void> task = new FutureTask<>(() -> {
            ScheduledFuture<?> expiry = null;
            JSONObject result = new JSONObject().put("type", "result").put("id", id);
            try {
                if (SystemClock.elapsedRealtime() >= deadline || !requestSession.equals(session)) throw new IOException("请求已过期");
                expiry = timers.schedule(() -> cancel(id), Math.max(1, deadline - SystemClock.elapsedRealtime()), TimeUnit.MILLISECONDS);
                JSONObject params = message.getJSONObject("params"), data;
                switch (message.getString("op")) {
                    case "foreground": data = local.foreground(id); break;
                    case "shell": data = local.shell(id, params.getString("command"), Math.min(8 * 1024 * 1024, params.optInt("limit", 8192))); break;
                    case "nodes": data = local.nodes(id, 8 * 1024 * 1024); break;
                    case "media_start": data = currentRecorder.start(params); break;
                    case "media_status": data = currentRecorder.status(params.getString("recording_id")); break;
                    case "media_stop": data = currentRecorder.stop(params.getString("recording_id")); break;
                    case "media_delete": currentRecorder.delete(params.getString("recording_id")); data = new JSONObject().put("ok", true); break;
                    case "stream_open": currentStreams.open(id, params.getString("stream_id"), params.getString("service"),params.optBoolean("flow_control",false),params.optBoolean("frame_ack",false),params.optBoolean("download_credit",false)); data = new JSONObject().put("ok", true); break;
                    case "screen": data = local.screen(); break;
                    case "screen_config": local.configureVideo(params.getBoolean("enabled"), params.optBoolean("standby"),params); data = new JSONObject().put("ok", true).put("elapsed_ms",SystemClock.elapsedRealtime()); break;
                    case "clock": data = new JSONObject().put("elapsed_ms",SystemClock.elapsedRealtime()); break;
                    case "traffic_prepare": data = currentTraffic.prepare(); break;
                    case "traffic_start": data = currentTraffic.start(params, () -> proxyConnection(params.getString("capture_id"))); break;
                    case "traffic_status": data = currentTraffic.status(); break;
                    case "traffic_stop": currentTraffic.stopOwned(params.getString("capture_id")); data = new JSONObject().put("running", false); break;
                    case "clipboard": data = local.clipboard(); break;
                    case "input": data = local.input(id, params.getString("text"), params.optBoolean("press_enter")); local.invalidateVideo(); break;
                    default: throw new IOException("不支持的控制能力");
                }
                if (message.getString("op").equals("foreground") || message.getString("op").equals("shell") || message.getString("op").equals("nodes") || message.getString("op").equals("input"))
                    data.put("video_generation",local.videoGeneration());
                for (Iterator<String> keys = data.keys(); keys.hasNext();) { String key = keys.next(); result.put(key, data.get(key)); }
                result.put("rpc_ok", true);
            } catch (Exception e) { result.put("rpc_ok", false).put("error", e.getMessage() == null ? "本机 ADB 请求失败" : e.getMessage()); }
            finally { if (expiry != null) expiry.cancel(false); jobs.remove(id); }
            if (requestSession.equals(session) && !Thread.currentThread().isInterrupted()) send(result);
            return null;
        });
        jobs.put(id, task); work.execute(task);
    }
    private void cancel(String id) {
        FutureTask<Void> task = jobs.remove(id);
        if (task != null) task.cancel(true);
        LocalStreams data = streams; if (data != null) data.cancel(id);
        LocalAdb local = adb; if (local != null) local.cancel(id);
    }
    static JSONObject read(DataInputStream input) throws Exception {
        int size = input.readInt();
        if (size < 1 || size > 12 * 1024 * 1024) throw new IOException("消息长度无效");
        byte[] data = new byte[size]; input.readFully(data);
        return new JSONObject(new String(data, "UTF-8"));
    }
    private synchronized void send(JSONObject message) throws Exception {
        byte[] data = message.toString().getBytes("UTF-8");
        DataOutputStream out = output;
        if (out == null) throw new IOException("插件未连接");
        out.writeInt(data.length); out.write(data); out.flush();
    }
    private JSONObject requestUpdate(int offset) throws Exception {
        String id = UUID.randomUUID().toString();
        CompletableFuture<JSONObject> future = new CompletableFuture<>(); downloads.put(id, future);
        try {
            JSONObject request = new JSONObject().put("type", "update").put("id", id);
            if (offset >= 0) request.put("offset", offset);
            send(request); return future.get(30, TimeUnit.SECONDS);
        } finally { downloads.remove(id); }
    }
    private void installUpdate() throws Exception {
        JSONObject info = requestUpdate(-1).getJSONObject("info");
        if (info.getInt("versionCode") <= UpdateInstaller.version(getPackageManager().getPackageInfo(getPackageName(), 0)))
            throw new IOException("当前已是插件提供的版本");
        int size = info.getInt("size");
        if (size < 1 || size > 32 * 1024 * 1024) throw new IOException("安装包大小无效");
        File file = new File(getCacheDir(), "relay-update.apk");
        MessageDigest hash = MessageDigest.getInstance("SHA-256");
        try (OutputStream out = new FileOutputStream(file)) {
            for (int offset = 0; offset < size;) {
                JSONObject response = requestUpdate(offset);
                if (response.getInt("offset") != offset || !response.getJSONObject("info").toString().equals(info.toString())) throw new IOException("更新包已变化，请重新检查");
                byte[] data = Base64.decode(response.getString("data"), Base64.NO_WRAP);
                if (data.length == 0 || offset + data.length > size) throw new IOException("更新包不完整");
                out.write(data); hash.update(data); offset += data.length;
            }
        }
        if (!hex(hash.digest()).equals(info.getString("sha256"))) { file.delete(); throw new IOException("安装包摘要校验失败"); }
        UpdateInstaller.prepare(this, file, info.getInt("versionCode"));
        status = "安装包已验证，请在 App 中确认系统安装";
    }
    static String hex(byte[] data) {
        StringBuilder b = new StringBuilder(); for (byte value : data) b.append(String.format(Locale.ROOT, "%02x", value & 255)); return b.toString();
    }
    private void closeSocket() { SSLSocket s = socket; socket = null; if (s != null) try { s.close(); } catch (IOException e) { android.util.Log.w("PhoneRelay", "关闭连接失败", e); } }
    private void cleanupConnection() {
        synchronized(cleanupLock) {
        synchronized(ControlService.class) {if(activeService==this)connected=false;}
        session = null; closeSocket(); output = null;
        for (String id : jobs.keySet()) cancel(id);
        for (CompletableFuture<JSONObject> future : downloads.values()) future.completeExceptionally(new IOException("插件已断线"));
        PhoneTraffic capture = traffic; traffic = null; connectionCode = null;
        if (capture != null) capture.close();
        LocalStreams data = streams; streams = null; if (data != null) data.close();
        PhoneRecorder media = recorder; recorder = null; if (media != null) media.close();
        LocalAdb local = adb; adb = null;
        if (local != null) try { local.close(); } catch (IOException e) { android.util.Log.w("PhoneRelay", "关闭本机 ADB 失败", e); }
        }
    }
    @Override public void onDestroy() {
        CompletableFuture<Void> completion=new CompletableFuture<>();
        synchronized(ControlService.class) {
            lastCleanup=completion;
            running=false;
            if(activeService==this) {activeService=null;connected=false;status="App 控制已停止";}
        }
        if (network != null) network.shutdownNow();
        if (work != null) work.shutdownNow();
        if (timers != null) timers.shutdownNow();
        // TLS close_notify、本机ADB与媒体收尾均在工作线程执行。
        Thread cleanup=new Thread(()-> {
            try {cleanupConnection();completion.complete(null);}
            catch(RuntimeException e) {completion.completeExceptionally(e);android.util.Log.e("PhoneRelay","控制服务收尾失败",e);}
        },"relay-control-cleanup");cleanup.start();
        super.onDestroy();
    }
    @Override public IBinder onBind(Intent intent) { return null; }
}
