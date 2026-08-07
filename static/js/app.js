/* ============================================================
 * Lunar X WebUI 入口（Vue 3 + Element Plus，MD3 Expressive）
 * ============================================================ */

/* ---------- 全局工具 ---------- */
window.fmtBytes = function (b) {
    if (b == null || isNaN(b)) return '-';
    if (b < 1024) return b + ' B';
    const units = ['KB', 'MB', 'GB', 'TB'];
    let v = b;
    for (const u of units) {
        v /= 1024;
        if (v < 1024) return v.toFixed(v < 10 ? 2 : 1) + ' ' + u;
    }
    return v.toFixed(2) + ' PB';
};
window.fmtDuration = function (s) {
    s = Math.floor(s || 0);
    if (s < 60) return s + '秒';
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (h === 0) return m + '分钟';
    return h + '小时' + m + '分';
};
window.fmtTime = function (t) {
    if (!t) return '-';
    const d = new Date(t * 1000);
    const p = n => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
};

/* ---------- 主题 ---------- */
const THEMES = [
    { id: 'pink', label: '粉', color: '#EEA4AF', dark: true },
    { id: 'blue', label: '蓝', color: '#5BC9F3', dark: true },
    { id: 'white', label: '白', color: '#f4f5f7', dark: false },
];
function applyTheme(theme) {
    document.body.dataset.theme = theme;
    document.documentElement.classList.toggle('dark', (THEMES.find(t => t.id === theme) || THEMES[0]).dark);
    localStorage.setItem('lunarx_theme', theme);
}
window.setTheme = applyTheme;

/* ---------- 根组件 ---------- */
const App = {
    name: 'App',
    template: `
    <div class="app-shell">
      <!-- 侧边栏 -->
      <aside class="sidebar">
        <div class="sidebar-brand">
          <h1>Lunar X</h1>
          <small>控制面板</small>
        </div>

        <div class="nav-item" v-for="item in menu" :key="item.id"
             :class="{ active: page === item.id }" @click="go(item.id)">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </div>

        <div class="sidebar-spacer"></div>

        <div class="sidebar-section">外观</div>
        <div class="theme-dots">
          <div v-for="t in themes" :key="t.id" class="theme-dot" :class="[t.id, { active: theme === t.id }]"
               :title="t.label" @click="setTheme(t.id)"></div>
        </div>

        <div class="sidebar-section">账户</div>
        <div class="user-chip" @click="passwordVisible = true">
          <div class="user-avatar">{{ avatarChar }}</div>
          <div class="user-meta">
            <div class="name">{{ username || '未登录' }}</div>
            <div class="hint">点击修改密码</div>
          </div>
        </div>

        <div class="sidebar-footer">
          <span class="ver">v{{ version }}</span>
          <el-button size="small" text type="primary" @click="doRestart" style="margin-left:auto;">重启机器人</el-button>
          <el-button size="small" text @click="doLogout">退出</el-button>
        </div>
      </aside>

      <!-- 主内容 -->
      <main class="main-content">
        <component :is="currentComp" :key="page" />
      </main>

      <!-- 改密码弹窗 -->
      <el-dialog v-model="passwordVisible" title="修改密码" width="420px">
        <el-form label-position="top">
          <el-form-item label="旧密码">
            <el-input v-model="pw.old" type="password" show-password autocomplete="current-password" />
          </el-form-item>
          <el-form-item label="新密码">
            <el-input v-model="pw.new1" type="password" show-password autocomplete="new-password" />
          </el-form-item>
          <el-form-item label="确认新密码">
            <el-input v-model="pw.new2" type="password" show-password autocomplete="new-password" @keyup.enter="doChangePassword" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="passwordVisible = false">取消</el-button>
          <el-button type="primary" :loading="pwSaving" @click="doChangePassword">确认修改</el-button>
        </template>
      </el-dialog>
    </div>
    `,
    data() {
        return {
            page: 'dashboard',
            username: '',
            version: '0.0.0',
            theme: localStorage.getItem('lunarx_theme') || 'pink',
            themes: THEMES,
            menu: [
                { id: 'dashboard', label: '数据看板', icon: 'Odometer' },
                { id: 'protocol', label: '协议端配置', icon: 'Connection' },
                { id: 'bot', label: 'Bot 端配置', icon: 'Setting' },
                { id: 'plugins', label: '插件管理', icon: 'Box' },
                { id: 'users', label: '用户管理', icon: 'User' },
                { id: 'persona', label: '人格设置', icon: 'ChatDotRound' },
                { id: 'console', label: '控制台', icon: 'Monitor' },
            ],
            passwordVisible: false,
            pwSaving: false,
            pw: { old: '', new1: '', new2: '' },
        };
    },
    computed: {
        currentComp() { return window.Pages[this.page.charAt(0).toUpperCase() + this.page.slice(1)]; },
        avatarChar() {
            return this.username ? this.username.charAt(0).toUpperCase() : '?';
        },
    },
    watch: {
        page() {
            const c = this.currentComp;
            if (c && c.onShow) c.onShow();
        },
    },
    created() {
        applyTheme(this.theme);
        window.addEventListener('hashchange', this.onHash);
        this.onHash();
        this.loadVersion();
        this.loadUser();
    },
    beforeUnmount() {
        window.removeEventListener('hashchange', this.onHash);
    },
    methods: {
        async loadUser() {
            try {
                const resp = await fetch('/api/auth_status', { cache: 'no-store' });
                const data = await resp.json();
                if (data.logged_in && data.username) this.username = data.username;
            } catch (e) { /* 忽略 */ }
        },
        onHash() {
            const h = (location.hash || '#/dashboard').replace(/^#\//, '');
            if (this.menu.some(m => m.id === h)) this.page = h;
            else this.page = 'dashboard';
        },
        go(id) {
            if (this.page === id) return;
            this.page = id;
            location.hash = '#/' + id;
        },
        setTheme(t) { this.theme = t; applyTheme(t); },
        async loadVersion() {
            try {
                const resp = await fetch('/api/version', { cache: 'no-store' });
                const data = await resp.json();
                this.version = data.version || '0.0.0';
            } catch (e) { /* 忽略 */ }
        },
        doRestart() {
            this.$confirm('确定重启机器人进程？连接会短暂中断。', '重启确认', { type: 'warning' })
                .then(async () => {
                    try {
                        const resp = await fetch('/api/restart_bot', { method: 'POST' });
                        const data = await resp.json();
                        if (resp.ok) this.$message.success(data.message || '重启中');
                        else this.$message.error(data.error || '重启失败');
                    } catch (e) {
                        this.$message.error('重启失败: ' + e.message);
                    }
                })
                .catch(() => {});
        },
        doLogout() {
            fetch('/api/logout', { method: 'POST' }).finally(() => {
                location.href = '/login';
            });
        },
        async doChangePassword() {
            if (!this.pw.old || !this.pw.new1) { this.$message.warning('请填写旧密码和新密码'); return; }
            if (this.pw.new1 !== this.pw.new2) { this.$message.warning('两次输入的新密码不一致'); return; }
            this.pwSaving = true;
            try {
                const resp = await fetch('/api/change_password', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ old_password: this.pw.old, new_password: this.pw.new1 }),
                });
                const data = await resp.json();
                if (resp.ok) {
                    this.$message.success(data.message || '密码已修改');
                    this.passwordVisible = false;
                    this.pw = { old: '', new1: '', new2: '' };
                } else {
                    this.$message.error(data.error || '修改失败');
                }
            } catch (e) {
                this.$message.error('修改失败: ' + e.message);
            } finally {
                this.pwSaving = false;
            }
        },
    },
};

/* ---------- 启动 ---------- */
async function boot() {
    // 登录校验
    try {
        const resp = await fetch('/api/auth_status', { cache: 'no-store' });
        const data = await resp.json();
        if (!data.logged_in) { location.href = '/login'; return; }
    } catch (e) {
        location.href = '/login';
        return;
    }

    const app = Vue.createApp(App);
    app.use(ElementPlus);
    // 全局注册 Element Plus 图标
    for (const [name, comp] of Object.entries(ElementPlusIconsVue)) {
        app.component(name, comp);
    }
    // 全局注册页面组件
    for (const [name, comp] of Object.entries(window.Pages || {})) {
        app.component('Page' + name, comp);
    }
    app.mount('#app');
}

boot();
