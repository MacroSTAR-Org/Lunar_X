/* 数据看板 */
window.Pages = window.Pages || {};
window.Pages.Dashboard = {
    name: 'DashboardPage',
    template: `
    <div>
      <h2 class="page-title">数据看板</h2>
      <p class="page-sub">框架、协议端与服务器的实时运行状态（每 10 秒自动刷新）</p>

      <div class="stat-grid">
        <el-card class="stat-card" shadow="never">
          <div class="stat-label">QQ 在线状态</div>
          <div class="stat-value" :class="{'stat-accent': d.qq && d.qq.online}">
            {{ d.qq && d.qq.online ? '在线' : '离线' }}
          </div>
          <div class="stat-sub">{{ (d.qq && d.qq.detail) || '加载中...' }}</div>
        </el-card>

        <el-card class="stat-card" shadow="never">
          <div class="stat-label">Bot 进程</div>
          <div class="stat-value" :class="{'stat-accent': d.bot && d.bot.running}">
            {{ d.bot && d.bot.running ? '运行中' : '未运行' }}
          </div>
          <div class="stat-sub">
            <template v-if="d.bot && d.bot.running">PID {{ d.bot.pid }} · 已运行 {{ fmtDuration(d.bot.uptime) }}</template>
            <template v-else>Lunar X 主进程未启动</template>
          </div>
        </el-card>

        <el-card class="stat-card" shadow="never">
          <div class="stat-label">系统 CPU 占用</div>
          <div class="stat-value" :style="cpuHot ? 'color:#ff7b72' : ''">
            {{ sys.cpu_percent != null ? sys.cpu_percent.toFixed(1) + '%' : '-' }}
          </div>
          <div class="stat-sub">实时采样</div>
        </el-card>

        <el-card class="stat-card" shadow="never">
          <div class="stat-label">系统内存占用</div>
          <div class="stat-value">{{ sys.memory_percent != null ? sys.memory_percent.toFixed(1) + '%' : '-' }}</div>
          <div class="stat-sub">
            {{ sys.memory_used != null ? fmtBytes(sys.memory_used) + ' / ' + fmtBytes(sys.memory_total) : '' }}
          </div>
        </el-card>

        <el-card class="stat-card" shadow="never">
          <div class="stat-label">Bot 内存占用</div>
          <div class="stat-value">{{ d.bot && d.bot.memory_bytes ? fmtBytes(d.bot.memory_bytes) : '-' }}</div>
          <div class="stat-sub">Lunar X 进程常驻内存</div>
        </el-card>

        <el-card class="stat-card" shadow="never">
          <div class="stat-label">Bot CPU（今日）</div>
          <div class="stat-value">
            {{ d.bot && d.bot.cpu_seconds_today != null ? fmtDuration(d.bot.cpu_seconds_today) : '-' }}
          </div>
          <div class="stat-sub">
            {{ d.bot && d.bot.cpu_seconds != null ? '累计 ' + fmtDuration(d.bot.cpu_seconds) : '' }}
          </div>
        </el-card>

        <el-card class="stat-card" shadow="never">
          <div class="stat-label">数据占用量</div>
          <div class="stat-value">{{ d.storage ? fmtBytes(d.storage.total) : '-' }}</div>
          <div class="stat-sub" v-if="d.storage">
            core: {{ fmtBytes(d.storage.dirs.core) }} · plugins: {{ fmtBytes(d.storage.dirs.plugins) }}
            · uploads: {{ fmtBytes(d.storage.dirs.uploads) }}
          </div>
        </el-card>

        <el-card class="stat-card" shadow="never">
          <div class="stat-label">系统磁盘占用</div>
          <div class="stat-value" :style="diskHot ? 'color:#ff7b72' : ''">
            {{ sys.disk_percent != null ? sys.disk_percent.toFixed(1) + '%' : '-' }}
          </div>
          <div class="stat-sub">
            {{ sys.disk_used != null ? fmtBytes(sys.disk_used) + ' / ' + fmtBytes(sys.disk_total) : '' }}
          </div>
        </el-card>

        <el-card class="stat-card" shadow="never">
          <div class="stat-label">7 天消息总数</div>
          <div class="stat-value stat-accent">{{ d.messages ? d.messages.total_7d : '-' }}</div>
          <div class="stat-sub">
            接收 + 发送
            <template v-if="sys.boot_time"> · 开机 {{ fmtTime(sys.boot_time) }}</template>
          </div>
        </el-card>
      </div>

      <el-card shadow="never" style="margin-top: 16px;">
        <template #header>近 7 天消息趋势</template>
        <div ref="chartEl" class="chart-box"></div>
      </el-card>
    </div>
    `,
    data() {
        return {
            d: {},
            timer: null,
        };
    },
    computed: {
        sys() { return this.d.system || {}; },
        cpuHot() { return this.sys.cpu_percent > 80; },
        diskHot() { return this.sys.disk_percent > 85; },
    },
    mounted() {
        try {
            this.chart = echarts.init(this.$refs.chartEl);
        } catch (e) {
        }
        window.addEventListener('resize', this.onResize);
        this.load();
        this.timer = setInterval(this.load, 10000);
    },
    beforeUnmount() {
        if (this.timer) clearInterval(this.timer);
        window.removeEventListener('resize', this.onResize);
        if (this.chart) { this.chart.dispose(); this.chart = null; }
    },
    methods: {
        onResize() { if (this.chart) this.chart.resize(); },
        async load() {
            try {
                const resp = await fetch('/api/dashboard', { cache: 'no-store' });
                if (resp.status === 401) { location.href = '/login'; return; }
                this.d = await resp.json();
                this.renderChart();
            } catch (e) {
            }
        },
        renderChart() {
            if (!this.d.messages || !this.d.messages.days || !this.chart) return;
            const days = this.d.messages.days;
            const dates = days.map(x => x.date.slice(5));
            const received = days.map(x => x.received);
            const sent = days.map(x => x.sent);
            const total = days.map(x => x.received + x.sent);
            const isDark = document.body.dataset.theme !== 'white';
            const textColor = isDark ? '#a0a1a8' : '#64686f';
            const primary = getComputedStyle(document.documentElement).getPropertyValue('--el-color-primary').trim() || '#5BC9F3';
            this.chart.setOption({
                backgroundColor: 'transparent',
                tooltip: {
                    trigger: 'axis',
                    backgroundColor: isDark ? '#23242c' : '#ffffff',
                    borderColor: isDark ? '#3a3b44' : '#e2e4e9',
                    textStyle: { color: isDark ? '#e8e8ea' : '#24262b' },
                },
                legend: {
                    data: ['接收', '发送', '合计'],
                    textStyle: { color: textColor },
                    top: 0,
                },
                grid: { left: 42, right: 16, top: 36, bottom: 30 },
                xAxis: {
                    type: 'category',
                    data: dates,
                    axisLine: { lineStyle: { color: isDark ? '#3a3b44' : '#d3d6dd' } },
                    axisLabel: { color: textColor },
                },
                yAxis: {
                    type: 'value',
                    minInterval: 1,
                    splitLine: { lineStyle: { color: isDark ? '#2a2b35' : '#eef0f3', type: 'dashed' } },
                    axisLabel: { color: textColor },
                },
                series: [
                    { name: '接收', type: 'bar', stack: 'm', data: received, barWidth: '42%',
                      itemStyle: { color: primary, opacity: 0.85, borderRadius: [6, 6, 0, 0] } },
                    { name: '发送', type: 'bar', stack: 'm', data: sent, barWidth: '42%',
                      itemStyle: { color: isDark ? '#9fd8e8' : '#006a86', opacity: 0.65, borderRadius: [6, 6, 0, 0] } },
                    { name: '合计', type: 'line', data: total, smooth: true, symbolSize: 6,
                      lineStyle: { width: 2.5, color: primary },
                      itemStyle: { color: primary } },
                ],
            });
        },
    },
};
