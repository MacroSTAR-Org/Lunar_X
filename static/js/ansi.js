/* ANSI 着色工具（控制台日志用） */
window.consoleEscapeHtml = function (s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
};

window.ansiToHtml = function (text) {
    const colors = {30:'#9aa0a8',31:'#ff7b72',32:'#7ee787',33:'#f2cc60',34:'#79c0ff',35:'#d2a8ff',36:'#a5d6ff',37:'#e6edf3',90:'#8b949e',91:'#ff7b72',92:'#7ee787',93:'#f2cc60',94:'#79c0ff',95:'#d2a8ff',96:'#a5d6ff',97:'#ffffff'};
    return text.replace(/\x1b\[([0-9;]*)m/g, (match, code) => {
        if (!code || code === '0') return '</span>';
        const parts = code.split(';').map(Number);
        if (parts.includes(1)) return '<span style="font-weight: bold;">';
        const colorCode = parts.find(p => p >= 30 && p < 38) || parts.find(p => p >= 90 && p < 98);
        if (colorCode !== undefined) return '<span style="color: ' + colors[colorCode] + ';">';
        return '';
    });
};
