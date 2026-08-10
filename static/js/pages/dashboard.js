/* ============================================================
 * 数据看板
 * 9 张指标卡 + 近 7 天消息趋势图，10 秒轮询
 *
 * 后端 /api/dashboard 的 system / bot 两组键在异常时会整组消失，
 * 所以统一走 computed 兜底，模板里不直接点多层属性。
 * ============================================================ */
window.Pages = window.Pages || {};

window.Pages.Dashboard = {
  name: 'DashboardPage',
  template: `
  <div>
    <div class="stat-grid">
      <el-card shadow="never" class="stat-card" :class="{ 'stat-accent': qq.online }">
        <div class="stat-label">QQ 在线状态</div>
        <div class="stat-value">
          <span class="dot" :class="qq.online ? 'on' : 'off'"></span>{{ qq.online ? '在线' : '离线' }}
        </div>
        <div class="stat-sub">{{ qq.detail || '加载中...' }}</div>
      </el-card>

      <el-card shadow="never" class="stat-card" :class="{ 'stat-accent': bot.running }">
        <div class="stat-label">Bot 进程</div>
        <div class="stat-value">
          <span class="dot" :class="bot.running ? 'on' : 'off'"></span>{{ bot.running ? '运行中' : '未运行' }}
        </div>
        <div class="stat-sub" v-if="bot.running">PID {{ bot.pid }} · 已运行 {{ fmtDuration(bot.uptime) }}</div>
        <div class="stat-sub" v-else>Lunar X 主进程未启动</div>
      </el-card>

      <el-card shadow="never" class="stat-card" :class="{ 'stat-danger': cpuHot }">
        <div class="stat-label">系统 CPU 占用</div>
        <div class="stat-value">{{ sys.cpu_percent != null ? sys.cpu_percent.toFixed(1) + '%' : '-' }}</div>
        <div class="stat-sub">实时采样</div>
      </el-card>

      <el-card shadow="never" class="stat-card">
        <div class="stat-label">系统内存占用</div>
        <div class="stat-value">{{ sys.memory_percent != null ? sys.memory_percent.toFixed(1) + '%' : '-' }}</div>
        <div class="stat-sub">{{ fmtBytes(sys.memory_used) }} / {{ fmtBytes(sys.memory_total) }}</div>
      </el-card>

      <el-card shadow="never" class="stat-card">
        <div class="stat-label">Bot 内存占用</div>
        <div class="stat-value">{{ fmtBytes(bot.memory_bytes) }}</div>
        <div class="stat-sub">Lunar X 进程常驻内存</div>
      </el-card>

      <el-card shadow="never" class="stat-card">
        <div class="stat-label">Bot CPU（今日）</div>
        <div class="stat-value">{{ fmtDuration(bot.cpu_seconds_today) }}</div>
        <div class="stat-sub">累计 {{ fmtDuration(bot.cpu_seconds) }}</div>
      </el-card>

      <el-card shadow="never" class="stat-card">
        <div class="stat-label">数据占用量</div>
        <div class="stat-value">{{ fmtBytes(storage.total) }}</div>
        <div class="stat-sub" v-if="storage.dirs">
          core {{ fmtBytes(storage.dirs.core) }} · plugins {{ fmtBytes(storage.dirs.plugins) }} · uploads {{ fmtBytes(storage.dirs.uploads) }}
        </div>
      </el-card>

      <el-card shadow="never" class="stat-card" :class="{ 'stat-danger': diskHot }">
        <div class="stat-label">系统磁盘占用</div>
        <div class="stat-value">{{ sys.disk_percent != null ? sys.disk_percent.toFixed(1) + '%' : '-' }}</div>
        <div class="stat-sub">{{ fmtBytes(sys.disk_used) }} / {{ fmtBytes(sys.disk_total) }}</div>
      </el-card>

      <el-card shadow="never" class="stat-card stat-accent">
        <div class="stat-label">7 天消息总数</div>
        <div class="stat-value">{{ messages.total_7d != null ? messages.total_7d : '-' }}</div>
        <div class="stat-sub">
          接收 + 发送<template v-if="sys.boot_time"> · 开机 {{ fmtTime(sys.boot_time) }}</template>
        </div>
      </el-card>
    </div>

    <el-card shadow="never">
      <template #header>近 7 天消息趋势</template>
      <div ref="chartEl" class="chart-box"></div>
    </el-card>
  </div>
  `,

  data() {
    return { d: {}, timer: null, chart: null };
  },

  computed: {
    sys() { return this.d.system || {}; },
    bot() { return this.d.bot || {}; },
    qq() { return this.d.qq || {}; },
    storage() { return this.d.storage || {}; },
    messages() { return this.d.messages || {}; },
    cpuHot() { return this.sys.cpu_percent > 80; },
    diskHot() { return this.sys.disk_percent > 85; },
  },

  mounted() {
    try {
      this.chart = echarts.init(this.$refs.chartEl);
    } catch (e) { /* ECharts 初始化失败时看板其余部分仍可用 */ }

    this.load();
    this.applyRefresh();
    window.addEventListener('resize', this.onResize);
    // 明暗切换后立即重绘，不必等下一次轮询
    window.addEventListener('lunarx-theme-change', this.renderChart);
    // 刷新间隔偏好变化后重置定时器
    window.addEventListener('lunarx-prefs-change', this.applyRefresh);
  },

  beforeUnmount() {
    clearInterval(this.timer);
    window.removeEventListener('resize', this.onResize);
    window.removeEventListener('lunarx-theme-change', this.renderChart);
    window.removeEventListener('lunarx-prefs-change', this.applyRefresh);
    if (this.chart) { this.chart.dispose(); this.chart = null; }
  },

  methods: {
    onResize() { if (this.chart) this.chart.resize(); },

    /** 按偏好读取看板轮询间隔（秒），变化时重建定时器 */
    applyRefresh() {
      let interval = 10000;
      try {
        const raw = JSON.parse(localStorage.getItem('lunarx_prefs') || '{}');
        const s = Number(raw.dashboard_refresh);
        if (s >= 3 && s <= 300) interval = s * 1000;
      } catch (e) { /* 用默认值 */ }
      clearInterval(this.timer);
      this.timer = setInterval(this.load, interval);
    },

    async load() {
      try {
        this.d = await Api.get('/api/dashboard');
        this.renderChart();
      } catch (e) {
        // 轮询接口静默失败即可，不刷屏；401 已由 Api 层统一跳转
      }
    },

    renderChart() {
      const days = this.messages.days;
      if (!this.chart || !days) return;

      // 单色图表：两条柱子靠明度区分（实心白 / 半透明白），
      // 合计线用虚线，与页面的"线条+点阵"语言一致
      const isDark = document.body.dataset.theme !== 'light';
      const fg = isDark ? '#ffffff' : '#000000';
      const strong = isDark ? 'rgba(255,255,255,.92)' : 'rgba(0,0,0,.88)';
      const weak = isDark ? 'rgba(255,255,255,.34)' : 'rgba(0,0,0,.28)';
      const line = isDark ? 'rgba(255,255,255,.12)' : 'rgba(0,0,0,.12)';
      const textColor = isDark ? '#9a9a9a' : '#5f5f5f';
      const surface = isDark ? '#0c0c0c' : '#ffffff';

      this.chart.setOption({
        backgroundColor: 'transparent',
        animationDuration: 550,
        animationEasing: 'cubicOut',
        textStyle: { fontFamily: 'Segoe UI, PingFang SC, Microsoft YaHei, system-ui, sans-serif' },
        tooltip: {
          trigger: 'axis',
          backgroundColor: surface,
          borderColor: line,
          textStyle: { color: isDark ? '#ededed' : '#111111' },
        },
        legend: {
          data: ['接收', '发送', '合计'], top: 0,
          textStyle: { color: textColor, fontSize: 11 },
          itemWidth: 14, itemHeight: 8, itemGap: 18,
        },
        grid: { left: 42, right: 16, top: 36, bottom: 30 },
        xAxis: {
          type: 'category',
          data: days.map(x => x.date.slice(5)),
          axisLine: { lineStyle: { color: line } },
          axisTick: { show: false },
          axisLabel: { color: textColor, fontSize: 11 },
        },
        yAxis: {
          type: 'value',
          minInterval: 1,
          axisLine: { show: false },
          axisLabel: { color: textColor, fontSize: 11 },
          splitLine: { lineStyle: { color: line, type: 'dashed' } },
        },
        series: [
          {
            name: '接收', type: 'bar', stack: 'm', barWidth: '38%',
            data: days.map(x => x.received),
            itemStyle: { color: strong, borderRadius: [3, 3, 0, 0] },
          },
          {
            name: '发送', type: 'bar', stack: 'm',
            data: days.map(x => x.sent),
            itemStyle: { color: weak, borderRadius: [3, 3, 0, 0] },
          },
          {
            name: '合计', type: 'line', smooth: true,
            data: days.map(x => (x.received || 0) + (x.sent || 0)),
            symbol: 'circle', symbolSize: 5,
            lineStyle: { color: fg, width: 1.4, type: 'dashed' },
            itemStyle: { color: surface, borderColor: fg, borderWidth: 1.4 },
          },
        ],
      });
    },
  },
};
