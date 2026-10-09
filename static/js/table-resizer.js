/**
 * 表格列宽手动拖拽调整（V10.11.7）
 * ============================================================
 * 使用方式：
 *   1. 表格标记：<table data-resizable="表格唯一ID">
 *   2. 列标记：所有 <th data-col="列唯一key">（必填，列增减不错位的关键）
 *   3. 可选约束：<th data-col-min="40" data-col-max="200">（默认 min 60 / max 480）
 *   4. JS 动态生成的表格：渲染完成后调用 window.TableResizer.init(table元素)
 *
 * 核心机制：
 *   - 首次进入把各列「自然宽度」固化为显式像素 + table-layout: fixed，使拖拽实时生效
 *   - 每列 th 右缘注入 8px 命中分隔条（Pointer Events，鼠标/触摸统一）
 *   - 拖动过程 body 加 col-resizing 类：user-select:none 防误选表头文字
 *   - 总宽超容器时由 .table-responsive 出横向滚动条，不压缩其他列
 *   - 双击分隔条：自动适应该列内容最大宽度（同帧切换布局测量，无闪烁）
 *   - 持久化：localStorage 键 gift_colw_{用户ID}_{表格ID}，按「用户+表格」隔离
 *   - 重置入口：容器右上角自动注入「重置列宽」小按钮，清除存储恢复默认
 *   - 响应式降级：屏宽 < 768px 不启用（不拖拽、不恢复存储、自然布局）
 */
(function () {
    'use strict';

    // ===== 配置 =====
    var DEFAULT_MIN = 60;   // 默认最小列宽 px
    var DEFAULT_MAX = 480;  // 默认最大列宽 px
    var MOBILE_MAX = 767.98; // 降级断点（与 Bootstrap md 一致）
    var STORAGE_PREFIX = 'gift_colw_';

    // ===== 工具 =====
    function getUserId() {
        var body = document.body;
        return (body && body.dataset && body.dataset.userId) ? body.dataset.userId : '0';
    }

    function storageKey(tableId) {
        return STORAGE_PREFIX + getUserId() + '_' + tableId;
    }

    function isMobile() {
        return window.innerWidth < MOBILE_MAX || window.matchMedia('(pointer: coarse) and (max-width: 767.98px)').matches;
    }

    // ===== 单表控制器 =====
    function initTable(table) {
        if (!table || table.__colwInit) return;            // 幂等
        var tableId = table.getAttribute('data-resizable');
        if (!tableId) return;
        var headRow = table.querySelector('thead tr');
        if (!headRow) return;
        var ths = Array.prototype.slice.call(headRow.querySelectorAll('th[data-col]'));
        if (ths.length === 0) return;

        // 移动端降级：不启用拖拽，保持自然布局
        if (isMobile()) {
            teardown(table);
            return;
        }

        table.__colwInit = true;
        table.__colwId = tableId;

        // 1) 固化各列初始自然宽度（auto 布局下测量），作为「默认宽度」
        var defaults = {};
        ths.forEach(function (th) {
            var col = th.getAttribute('data-col');
            defaults[col] = Math.round(th.getBoundingClientRect().width);
        });
        table.__colwDefaults = defaults;

        // 2) 应用持久化的用户列宽（无记录则保持默认）
        applyStored(table, ths);

        // 3) 切换 fixed 布局 + 单行截断样式（CSS 配合）
        table.style.tableLayout = 'fixed';
        table.classList.add('colw-fixed');

        // 4) 注入分隔条与重置按钮
        ths.forEach(function (th) { attachResizer(table, th); });
        injectResetBtn(table);

        // 5) 截断单元格补 title 提示（hover 看全文）
        refreshCellTitles(table);
    }

    function applyStored(table, ths) {
        var key = storageKey(table.__colwId || table.getAttribute('data-resizable'));
        var saved = null;
        try { saved = JSON.parse(localStorage.getItem(key) || 'null'); } catch (e) { saved = null; }
        ths.forEach(function (th) {
            var col = th.getAttribute('data-col');
            var w = (saved && saved[col]) ? saved[col] : (table.__colwDefaults ? table.__colwDefaults[col] : th.getBoundingClientRect().width);
            th.style.width = clampCol(th, w) + 'px';
        });
    }

    function colMin(th) {
        var v = parseFloat(th.getAttribute('data-col-min'));
        return isNaN(v) ? DEFAULT_MIN : v;
    }

    function colMax(th) {
        var v = parseFloat(th.getAttribute('data-col-max'));
        return isNaN(v) ? DEFAULT_MAX : v;
    }

    function clampCol(th, w) {
        var mn = colMin(th), mx = colMax(th);
        return Math.max(mn, Math.min(mx, w));
    }

    function attachResizer(table, th) {
        if (th.querySelector('.colw-resizer')) return;
        var bar = document.createElement('div');
        bar.className = 'colw-resizer';
        bar.title = '拖动调整列宽；双击自动适应内容宽度';
        th.appendChild(bar);

        // ---- 拖拽（Pointer Events：鼠标/触摸统一）----
        bar.addEventListener('pointerdown', function (e) {
            e.preventDefault();
            e.stopPropagation();
            var startX = e.clientX;
            var startW = th.getBoundingClientRect().width;
            bar.setPointerCapture(e.pointerId);
            document.body.classList.add('colw-resizing');

            function onMove(ev) {
                var w = clampCol(th, startW + (ev.clientX - startX));
                th.style.width = w + 'px';
            }
            function onUp() {
                bar.removeEventListener('pointermove', onMove);
                bar.removeEventListener('pointerup', onUp);
                bar.removeEventListener('pointercancel', onUp);
                document.body.classList.remove('colw-resizing');
                saveWidths(table);
                refreshCellTitles(table);
            }
            bar.addEventListener('pointermove', onMove);
            bar.addEventListener('pointerup', onUp);
            bar.addEventListener('pointercancel', onUp);
        });

        // ---- 双击：自动适应内容宽度 ----
        bar.addEventListener('dblclick', function (e) {
            e.preventDefault();
            e.stopPropagation();
            var w = measureContentWidth(table, th);
            th.style.width = clampCol(th, w) + 'px';
            saveWidths(table);
            refreshCellTitles(table);
        });
    }

    // 同帧内切 auto 布局测该列内容自然最大宽，再切回 fixed（一帧内完成，无闪烁）
    function measureContentWidth(table, th) {
        var idx = Array.prototype.indexOf.call(th.parentNode.children, th);
        var cells = Array.prototype.slice.call(table.querySelectorAll('tbody tr'))
            .map(function (tr) { return tr.cells[idx]; })
            .filter(Boolean);
        // 兜底：至少用表头自身
        if (!cells.length) cells = [th];

        table.style.tableLayout = 'auto';
        table.classList.remove('colw-fixed');
        // 该列全部单元格临时放开宽度限制
        var old = cells.map(function (c) { return c.style.width; });
        cells.forEach(function (c) { c.style.width = 'auto'; });
        var maxW = 0;
        cells.forEach(function (c) { maxW = Math.max(maxW, c.scrollWidth); });
        maxW = Math.max(maxW, th.scrollWidth);
        // 恢复
        cells.forEach(function (c, i) { c.style.width = old[i]; });
        table.style.tableLayout = 'fixed';
        table.classList.add('colw-fixed');
        // 加一点余量，防止 ellipsis 恰好贴边
        return maxW + 16;
    }

    // 截断单元格 hover title 提示（初始化 / 拖拽后刷新）
    function refreshCellTitles(table) {
        var headRow = table.querySelector('thead tr');
        if (!headRow) return;
        var ths = Array.prototype.slice.call(headRow.querySelectorAll('th[data-col]'));
        ths.forEach(function (th, colIdx) {
            var rows = table.querySelectorAll('tbody tr');
            Array.prototype.forEach.call(rows, function (tr) {
                var td = tr.cells[colIdx];
                if (!td) return;
                var txt = (td.innerText || '').trim();
                // 内容实际超出可视宽度才需要 title
                if (txt && td.scrollWidth > td.clientWidth + 2) {
                    td.title = txt;
                } else {
                    td.removeAttribute('title');
                }
            });
        });
    }

    function saveWidths(table) {
        var headRow = table.querySelector('thead tr');
        if (!headRow) return;
        var data = {};
        headRow.querySelectorAll('th[data-col]').forEach(function (th) {
            var col = th.getAttribute('data-col');
            data[col] = Math.round(parseFloat(th.style.width) || th.getBoundingClientRect().width);
        });
        try {
            localStorage.setItem(storageKey(table.__colwId), JSON.stringify(data));
        } catch (e) { /* 存储满等异常静默，不影响功能 */ }
    }

    // 容器右上角注入「重置列宽」按钮
    function injectResetBtn(table) {
        var wrap = table.closest('.table-responsive') || table.parentElement;
        if (!wrap || wrap.querySelector('.colw-reset-btn')) return;
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn-outline-secondary btn-sm colw-reset-btn';
        btn.innerHTML = '<i class="fa-solid fa-rotate-left me-1"></i>重置列宽';
        btn.title = '清除已保存的列宽，恢复默认布局';
        btn.addEventListener('click', function () {
            try { localStorage.removeItem(storageKey(table.__colwId)); } catch (e) {}
            var headRow = table.querySelector('thead tr');
            if (headRow && table.__colwDefaults) {
                headRow.querySelectorAll('th[data-col]').forEach(function (th) {
                    var col = th.getAttribute('data-col');
                    th.style.width = clampCol(th, table.__colwDefaults[col]) + 'px';
                });
            }
            refreshCellTitles(table);
        });
        wrap.classList.add('colw-wrap');
        wrap.appendChild(btn);
    }

    // ===== 降级清理（移动端）=====
    function teardown(table) {
        if (!table) return;
        table.__colwInit = false;
        table.style.removeProperty('table-layout');
        table.classList.remove('colw-fixed');
        table.querySelectorAll('.colw-resizer').forEach(function (b) { b.remove(); });
        var wrap = table.closest('.table-responsive') || table.parentElement;
        if (wrap) {
            wrap.classList.remove('colw-wrap');
            var btn = wrap.querySelector('.colw-reset-btn');
            if (btn) btn.remove();
        }
        var headRow = table.querySelector('thead tr');
        if (headRow) {
            headRow.querySelectorAll('th[data-col]').forEach(function (th) {
                th.style.removeProperty('width');
            });
        }
    }

    // ===== 对外 API =====
    var TableResizer = {
        // 初始化单张表格（动态生成的表格在渲染后手动调用）
        init: function (table) { initTable(table); },
        // 全页扫描
        initAll: function () {
            document.querySelectorAll('table[data-resizable]').forEach(initTable);
        },
        // 视口跨越断点时重新适配
        rebind: function () {
            document.querySelectorAll('table[data-resizable]').forEach(function (t) {
                if (isMobile()) { teardown(t); }
                else { initTable(t); }
            });
        }
    };
    window.TableResizer = TableResizer;

    // ===== 启动 =====
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { TableResizer.initAll(); });
    } else {
        TableResizer.initAll();
    }
    // 视口在移动/桌面之间切换时自动启用/降级（防抖）
    var resizeTimer = null;
    window.addEventListener('resize', function () {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(function () { TableResizer.rebind(); }, 200);
    });
})();
