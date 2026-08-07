/* 控制台（实时日志） */
window.Pages = window.Pages || {};
window.Pages.Console = {
    name: 'ConsolePage',
    template: `
    <div>
      <h2 class="page-title">控制台</h2>
      <p class="page-sub">Lunar X 框架实时运行日志</p>

      <div class="console-toolbar">
        <el-checkbox v-model="autoScroll">自动滚动</el-checkbox>
        <el-button size="small" @click="clear">清空</el-button>
        <span style="flex:1"></span>
        <el-tag size="small" :type="connected ? 'success' : 'info'" effect="plain">
          {{ connected ? '实时连接中' : '重连中...' }}
        </el-tag>
      </div>
      <div ref="box" class="console-box"></div>
    </div>
    `,
    data() {
        return {
            autoScroll: true,
            connected: false,
            stream: null,
        };
    },
    mounted() {
        this.loadHistory();
        this.connect();
    },
    beforeUnmount() {
        if (this.stream) { this.stream.close(); this.stream = null; }
    },
    methods: {
        async loadHistory() {
            try {
                const resp = await fetch('/api/logs?tail=200', { cache: 'no-store' });
                if (resp.status === 401) { location.href = '/login'; return; }
                const data = await resp.json();
                const el = this.$refs.box;
                if (data.lines && data.lines.length) {
                    el.innerHTML = data.lines.map(l => ansiToHtml(consoleEscapeHtml(l))).join('\n');
                } else {
                    el.textContent = '（暂无日志，启动框架后这里会实时显示运行日志）';
                }
                el.scrollTop = el.scrollHeight;
            } catch (e) { /* 忽略 */ }
        },
        connect() {
            if (this.stream) this.stream.close();
            this.stream = new EventSource('/api/logs/stream');
            this.stream.onmessage = (e) => {
                const el = this.$refs.box;
                el.insertAdjacentHTML('beforeend', ansiToHtml(consoleEscapeHtml(e.data)) + '\n');
                if (this.autoScroll) el.scrollTop = el.scrollHeight;
            };
            this.stream.onopen = () => { this.connected = true; };
            this.stream.onerror = () => {
                this.connected = false;
                /* EventSource 自动重连 */
            };
        },
        clear() {
            this.$refs.box.innerHTML = '';
        },
    },
};
