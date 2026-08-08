/* ============================================================
 * 统一请求层
 *
 * 集中处理三件以前散落在各页面、且有遗漏的事：
 *   1. 401 → 跳登录页（之前插件开关/卸载/帮助/安装、改密、重启共 6 处漏判）
 *   2. cache: 'no-store'（浏览器会缓存慢接口的响应，导致数据不刷新）
 *   3. 错误信息提取：后端错误统一是 { error: "..." }
 *
 * 用法：
 *   const data = await Api.get('/api/dashboard');       // 失败抛 ApiError
 *   await Api.post('/api/config/config', payload);
 *   const ok = await Api.tryGet('/api/version', {});    // 失败返回兜底值，不抛
 * ============================================================ */
(function () {
  'use strict';

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
    }
  }

  function gotoLogin() {
    location.href = '/login';
  }

  async function request(method, url, body, options) {
    const opts = Object.assign({
      method: method,
      cache: 'no-store',
      headers: {},
    }, options || {});

    if (body !== undefined && body !== null) {
      opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers);
      opts.body = JSON.stringify(body);
    }

    let resp;
    try {
      resp = await fetch(url, opts);
    } catch (e) {
      throw new ApiError('网络错误: ' + e.message, 0);
    }

    if (resp.status === 401) {
      gotoLogin();
      throw new ApiError('未登录', 401);
    }

    // 有的接口（如 /api/logs 文件不存在）返回 200 但语义是空，交给调用方判断
    let data = null;
    const text = await resp.text();
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (e) {
        if (!resp.ok) throw new ApiError('服务端返回异常 (HTTP ' + resp.status + ')', resp.status);
        throw new ApiError('响应不是合法 JSON', resp.status);
      }
    }

    if (!resp.ok) {
      const msg = (data && (data.error || data.message)) || ('请求失败 (HTTP ' + resp.status + ')');
      throw new ApiError(msg, resp.status);
    }
    return data;
  }

  const Api = {
    ApiError: ApiError,

    get: (url, options) => request('GET', url, null, options),
    post: (url, body, options) => request('POST', url, body === undefined ? null : body, options),
    put: (url, body, options) => request('PUT', url, body === undefined ? null : body, options),
    del: (url, options) => request('DELETE', url, null, options),

    /** 失败时返回 fallback 而不抛异常（用于版本号这类非关键数据） */
    async tryGet(url, fallback) {
      try {
        return await request('GET', url, null, null);
      } catch (e) {
        return fallback;
      }
    },

    /**
     * POST + SSE 流式读取（浏览器 EventSource 只支持 GET，插件安装必须用这个）
     * onLine(text) 每收到一条 data: 帧回调一次
     * 返回 Promise，流结束时 resolve
     */
    async postStream(url, body, onLine) {
      const resp = await fetch(url, {
        method: 'POST',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (resp.status === 401) {
        gotoLogin();
        throw new ApiError('未登录', 401);
      }
      if (!resp.ok || !resp.body) {
        throw new ApiError('无法建立流式连接 (HTTP ' + resp.status + ')', resp.status);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const frames = buf.split('\n\n');
        buf = frames.pop();               // 最后一段可能不完整，留作下轮缓冲
        for (const frame of frames) {
          const line = frame.replace(/^data:\s*/, '').trim();
          if (line) onLine(line);
        }
      }
    },
  };

  window.Api = Api;
})();
