/* Mock 站页面签名: 采集器必须在页面上下文里调用, 不能在 Python 端硬算。
 * 算法刻意做了字符串拼接 + SHA-256, 模拟真实站点的混淆签名。 */
async function _sha256(message) {
  const buf = new TextEncoder().encode(message);
  const hash = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(hash))
    .map(b => b.toString(16).padStart(2, "0")).join("");
}

async function sign(ts, appNo) {
  const token = "page-secret-token-2026";
  const raw = token + "|" + ts + "|" + appNo;
  return await _sha256(raw);
}

// 同步包装, 方便采集器 evaluate 后直接 await; 页面业务用 asyncSign。
window.sign = sign;
