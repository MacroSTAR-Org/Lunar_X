/* ============================================================
 * 控制台（实时日志）
 *
 * 实现：长轮询而非 SSE。
 *   GET /api/logs?tail=200        取历史，同时拿到起始 offset
 *   GET /api/logs/tail?offset=N   挂起最多 20 秒，有新行立刻返回
 * 服务端 50ms 探测一次文件，端到端延迟约 50ms；历史与增量用同一个字节
 * 偏移量衔接，不会漏行也不会重复。
 *
 * 渲染：新行先进缓冲区，用 requestAnimationFrame 合并成一次 DOM 写入，
 * 并把总行数封顶，避免长时间挂着后 DOM 膨胀导致越来越卡。
 *
 * 转义顺序必须是「先 escapeHtml 再 ansiToHtml」，颠倒会导致 XSS。
 * ============================================================ */
window.Pages = window.Pages || {};

const MAX_LINES = 3000;          // DOM 里最多保留的行数

window.Pages.Console = {
  name: 'ConsolePage',
  template: `
  <div>
    <div class="console-toolbar">
      <el-checkbox v-model="autoScroll">自动滚动</el-checkbox>
      <el-button size="small" @click="clear">清空</el-button>
      <el-button size="small" :loading="reloading" @click="reload">重新加载</el-button>
      <span style="flex:1"></span>
      <span class="console-meta" v-if="lineCount">{{ lineCount }} 行<template v-if="lineCount >= max"> · 已达上限</template></span>
      <el-tag size="small" :type="statusTag.type" effect="plain">
        <span class="dot" :class="live ? 'on' : ''"></span>{{ statusTag.text }}
      </el-tag>
    </div>
    <div ref="box" class="console-box"></div>
  </div>
  `,

  data() {
    return {
      autoScroll: true,
      live: false,
      reloading: false,
      lineCount: 0,
      max: MAX_LINES,
      offset: -1,
      alive: false,          // 组件是否还挂载着，控制轮询循环退出
      abort: null,           // 用于在离开页面时立刻中断挂起的长轮询
      pending: [],           // 待渲染缓冲
      frame: null,
    };
  },

  computed: {
    statusTag() {
      return this.live
        ? { type: 'success', text: '实时' }
        : { type: 'info', text: '连接中…' };
    },
  },

  mounted() {
    this.alive = true;
    this.run();
  },

  beforeUnmount() {
    this.alive = false;
    if (this.abort) this.abort.abort();      // 不然挂起的请求要拖 20 秒才结束
    if (this.frame) cancelAnimationFrame(this.frame);
  },

  methods: {
    async run() {
      await this.loadHistory();
      this.poll();
    },

    async reload() {
      this.reloading = true;
      try {
        await this.loadHistory();
      } finally {
        this.reloading = false;
      }
    },

    async loadHistory() {
      try {
        const data = await Api.get('/api/logs?tail=200');
        const el = this.$refs.box;
        if (!el) return;
        this.offset = data.offset != null ? data.offset : -1;
        // 历史拉到了就说明服务器可达，直接标记为实时；
        // 否则空闲时首轮长轮询要挂满 20 秒，标签会一直卡在「连接中」
        this.live = true;
        if (data.lines && data.lines.length) {
          el.innerHTML = data.lines.map(l => ansiToHtml(consoleEscapeHtml(l))).join('\n') + '\n';
          this.lineCount = data.lines.length;
        } else {
          // 后端在日志文件不存在时也返回 200 + exists:false，不是 404
          el.textContent = '（暂无日志，启动框架后这里会实时显示运行日志）';
          this.lineCount = 0;
        }
        el.scrollTop = el.scrollHeight;
      } catch (e) {
        this.$message.error('加载日志失败：' + e.message);
      }
    },

    /** 长轮询循环：一轮结束立刻开下一轮，服务端负责挂起等待 */
    async poll() {
      while (this.alive) {
        this.abort = new AbortController();
        try {
          const data = await Api.get('/api/logs/tail?offset=' + this.offset,
                                     { signal: this.abort.signal });
          if (!this.alive) return;
          this.live = true;
          if (data.rotated) {
            // 日志被轮转或清空，重新拉一次历史，否则偏移量对不上
            await this.loadHistory();
            continue;
          }
          if (data.offset != null) this.offset = data.offset;
          if (data.lines && data.lines.length) this.push(data.lines);
        } catch (e) {
          if (!this.alive) return;         // 卸载导致的 abort，正常退出
          this.live = false;
          await new Promise(r => setTimeout(r, 1000));   // 出错退避后重试
        }
      }
    },

    /** 新行进缓冲，合并到下一帧统一写 DOM */
    push(lines) {
      this.pending.push(...lines);
      if (this.frame) return;
      this.frame = requestAnimationFrame(() => {
        this.frame = null;
        const el = this.$refs.box;
        const batch = this.pending;
        this.pending = [];
        if (!el || !batch.length) return;

        if (this.lineCount === 0) el.textContent = '';   // 清掉「暂无日志」占位
        el.insertAdjacentHTML('beforeend',
          batch.map(l => ansiToHtml(consoleEscapeHtml(l))).join('\n') + '\n');
        this.lineCount += batch.length;

        // 超出上限就从头裁掉，控制 DOM 体量
        if (this.lineCount > this.max) {
          const html = el.innerHTML.split('\n');
          el.innerHTML = html.slice(html.length - this.max).join('\n');
          this.lineCount = this.max;
        }
        if (this.autoScroll) el.scrollTop = el.scrollHeight;
      });
    },

    clear() {
      if (this.$refs.box) this.$refs.box.innerHTML = '';
      this.lineCount = 0;
    },
  },
};
