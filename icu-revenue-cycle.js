/**
 * ICU Billing Workflow Module - متابعة فوترة العناية المركزة (نفقة الدولة)
 *
 * Replaces the old "bill vs revenue" logic. Display-only front end for the data
 * written every night by icu_billing_tracker.py (GitHub workflow
 * icu-billing-tracker.yml). Nothing is computed against SMC here.
 *
 * Tabs
 *   A - الحالات      : one row per ICU stay, stage, next due date, delay, outcome
 *   B - سجل التأخيرات : permanent delay record (adherence reference), with excuses
 *
 * Writes (the only two things the app may change, enforced by DB grants):
 *   icu_stay_tracking : manual_status / manual_reason / manual_by / manual_at
 *   icu_stay_breaches : excused / excuse_reason / excused_by / excused_at
 *
 * Entry points kept for loader.js: window.icuRevenueCycleReady,
 * window.loadIcuRevenueCycle (called by navigateTo), window.ircSwitchTab.
 */

(function () {
    'use strict';
    console.log('🔄 ICU Billing Workflow Module loading...');
    window.icuRevenueCycleReady = false;

    const SUPABASE_URL = 'https://rwvxhoubyztyeeyhamrn.supabase.co';
    const PAGE_SIZE = 50;

    const STAGE_LABELS = {
        bill_creation:        'إنشاء فاتورة العناية',
        first_request:        'تقديم طلب القرار',
        awaiting_smc:         'بانتظار رد SMC',
        admin_letter:         'خطاب إداري - طلب جديد',
        cancelled_request:    'طلب ملغي - طلب جديد',
        bill_under_decree:    'فوترة الخدمات تحت القرار',
        continuation_request: 'طلب استكمال',
        data_pending:         'بيانات SMC غير مكتملة',
        closed:               'مغلق',
    };
    const SMC_LABELS = {
        no_request:   'لا يوجد طلب',
        pending:      'تحت الدراسة',
        issued:       'قرار نهائي',
        admin_letter: 'خطاب إداري',
        cancelled:    'ملغي',
    };

    let _rows = [];          // icu_stay_tracking
    let _breaches = [];      // icu_stay_breaches
    let _lastRun = null;     // icu_tracker_runs latest
    let _filtered = [];
    let _breachFiltered = [];
    let _page = 1;
    let _bPage = 1;
    let _tab = 'stays';
    let _loading = false;

    /* ─── helpers ───────────────────────────────────────────────────────────── */
    function esc(v) {
        return String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    function headers(extra) {
        return Object.assign({}, window.getHeaders ? window.getHeaders() : {}, extra || {});
    }
    function toast(msg, type) { if (window.showToast) window.showToast(msg, type || 'info'); }
    function money(v) {
        if (v == null || v === '') return '—';
        return Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    function fmtDate(d) {                       // yyyy-mm-dd -> dd-mm-yyyy
        if (!d) return '—';
        const p = String(d).slice(0, 10).split('-');
        return p.length === 3 ? `${p[2]}-${p[1]}-${p[0]}` : esc(d);
    }
    function fmtDateTime(ts) {                  // timestamptz -> dd-mm-yyyy hh:mm (Cairo)
        if (!ts) return '—';
        const dt = new Date(ts);
        if (isNaN(dt)) return '—';
        const parts = new Intl.DateTimeFormat('en-GB', {
            timeZone: 'Africa/Cairo', day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit', hour12: false,
        }).formatToParts(dt).reduce((o, p) => { o[p.type] = p.value; return o; }, {});
        return `${parts.day}-${parts.month}-${parts.year} ${parts.hour}:${parts.minute}`;
    }
    function todayCairo() {
        return new Intl.DateTimeFormat('en-CA', { timeZone: 'Africa/Cairo' }).format(new Date());
    }
    // Days after `due` up to `today` (inclusive), Fridays excluded - same rule as the script.
    function workDaysAfter(due, today) {
        if (!due || !today || due >= today) return 0;
        let n = 0;
        const d = new Date(due + 'T00:00:00Z');
        const end = new Date(today + 'T00:00:00Z');
        for (d.setUTCDate(d.getUTCDate() + 1); d <= end; d.setUTCDate(d.getUTCDate() + 1)) {
            if (d.getUTCDay() !== 5) n++;
        }
        return n;
    }
    function currentUserName() {
        const u = window.currentUser || window.user;
        return window.currentUserProfile?.full_name
            || window.currentUser?.user?.email?.split('@')[0]
            || (u && (u.name || u.email || u.username))
            || 'غير معروف';
    }
    function canAct() {
        let role = '';
        try { role = window.currentUserRole || localStorage.getItem('_cachedUserRole') || ''; } catch (e) { /* ignore */ }
        if (!role) return true;                       // sidebar is already role-gated
        return ['admin', 'manager', 'supervisor'].includes(role);
    }

    async function fetchAll(table, select, filters) {
        const out = [];
        const step = 1000;
        for (let off = 0; ; off += step) {
            const url = `${SUPABASE_URL}/rest/v1/${table}?select=${select}${filters ? '&' + filters : ''}`;
            const r = await fetch(url, { headers: headers({ Range: `${off}-${off + step - 1}`, 'Range-Unit': 'items' }) });
            if (!r.ok) throw new Error(`${table}: ${r.status} ${(await r.text()).slice(0, 200)}`);
            const b = await r.json();
            out.push(...b);
            if (b.length < step) break;
        }
        return out;
    }
    async function fetchWhere(table, filters, order) {
        const url = `${SUPABASE_URL}/rest/v1/${table}?select=*&${filters}${order ? '&order=' + order : ''}`;
        const r = await fetch(url, { headers: headers() });
        if (!r.ok) throw new Error(`${table}: ${r.status}`);
        return r.json();
    }
    async function patchRow(table, filters, body) {
        const r = await fetch(`${SUPABASE_URL}/rest/v1/${table}?${filters}`, {
            method: 'PATCH',
            headers: headers({ 'Content-Type': 'application/json', Prefer: 'return=minimal' }),
            body: JSON.stringify(body),
        });
        if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
    }

    /* ─── derived state ─────────────────────────────────────────────────────── */
    // effective: closed | waived | delayed | open
    function effective(r, today) {
        if (r.manual_status === 'waived') return 'waived';
        if (r.manual_status === 'closed' || r.status === 'closed') return 'closed';
        if (r.next_due_date && r.next_due_date < today) return 'delayed';
        return 'open';
    }
    function delayOf(r, today) {
        return effective(r, today) === 'delayed' ? workDaysAfter(r.next_due_date, today) : 0;
    }

    /* ─── section ───────────────────────────────────────────────────────────── */
    function createSection() {
        const main = document.querySelector('.flex-1.p-6.bg-gray-50.min-h-screen');
        if (!main) return false;
        if (document.getElementById('icuRevenueCycleSection')) return true;
        const sec = document.createElement('div');
        sec.id = 'icuRevenueCycleSection';
        sec.className = 'section hidden';
        sec.innerHTML = buildSectionHTML();
        main.appendChild(sec);
        return true;
    }

    function buildSectionHTML() {
        const inp = 'w-full p-2 border rounded-lg text-sm focus:ring-2 focus:ring-teal-400 outline-none bg-white';
        return `
        <div class="mb-5 flex flex-wrap justify-between items-center gap-4">
            <div>
                <h2 class="text-2xl font-bold text-slate-800">
                    <i class="ri-exchange-funds-line text-teal-600"></i>
                    متابعة فوترة العناية المركزة - نفقة الدولة
                </h2>
                <p class="text-sm text-gray-500 mt-1">
                    الفاتورة في النظام ← طلب القرار على SMC ← فوترة الخدمات تحت القرار ← الاستكمال حتى تغطية كامل الفاتورة
                </p>
                <p class="text-xs text-gray-500 mt-1" id="ircLastCheck">آخر فحص: —</p>
            </div>
            <div class="flex gap-2">
                <button onclick="ircReload()" class="bg-white border hover:bg-gray-50 text-gray-700 py-2 px-4 rounded-lg text-sm font-semibold flex items-center gap-2">
                    <i class="ri-refresh-line"></i> تحديث
                </button>
                <button onclick="ircExport()" class="bg-emerald-600 hover:bg-emerald-700 text-white py-2 px-4 rounded-lg text-sm font-semibold flex items-center gap-2">
                    <i class="ri-file-excel-2-line"></i> تصدير Excel
                </button>
            </div>
        </div>

        <div id="ircSummaryCards" class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-5"></div>

        <div class="bg-white rounded-2xl shadow overflow-hidden">
            <div class="flex border-b">
                <button onclick="ircSwitchTab('stays')" id="ircTabStays"
                    class="flex-1 py-3 px-4 text-sm font-semibold border-b-2 border-teal-500 text-teal-700 bg-teal-50 transition">
                    <i class="ri-hospital-line"></i> الحالات
                </button>
                <button onclick="ircSwitchTab('breaches')" id="ircTabBreaches"
                    class="flex-1 py-3 px-4 text-sm font-semibold border-b-2 border-transparent text-gray-500 hover:bg-gray-50 transition">
                    <i class="ri-alarm-warning-line"></i> سجل التأخيرات (التزام المستخدم)
                </button>
            </div>

            <div id="ircPanelStays" class="p-5">
                <div class="grid grid-cols-1 md:grid-cols-6 gap-3 mb-4">
                    <div class="md:col-span-2">
                        <label class="block text-xs font-semibold text-gray-600 mb-1">بحث (اسم / رقم قومي / رقم فاتورة)</label>
                        <input id="ircSearch" oninput="ircApplyFilters()" class="${inp}" placeholder="بحث...">
                    </div>
                    <div>
                        <label class="block text-xs font-semibold text-gray-600 mb-1">الحالة</label>
                        <select id="ircStatus" onchange="ircApplyFilters()" class="${inp}">
                            <option value="">الكل</option>
                            <option value="delayed">متأخر</option>
                            <option value="open">مفتوح في الموعد</option>
                            <option value="closed">مغلق</option>
                            <option value="waived">مُعفى</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-semibold text-gray-600 mb-1">المرحلة</label>
                        <select id="ircStage" onchange="ircApplyFilters()" class="${inp}">
                            <option value="">الكل</option>
                            ${Object.keys(STAGE_LABELS).filter(k => k !== 'closed').map(k => `<option value="${k}">${STAGE_LABELS[k]}</option>`).join('')}
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-semibold text-gray-600 mb-1">خروج من</label>
                        <input type="date" id="ircFrom" onchange="ircApplyFilters()" class="${inp}">
                    </div>
                    <div>
                        <label class="block text-xs font-semibold text-gray-600 mb-1">خروج إلى</label>
                        <input type="date" id="ircTo" onchange="ircApplyFilters()" class="${inp}">
                    </div>
                </div>
                <div id="ircStaysTable"></div>
                <div id="ircStaysPager" class="flex justify-center items-center gap-3 mt-4 text-sm"></div>
            </div>

            <div id="ircPanelBreaches" class="p-5 hidden">
                <div class="grid grid-cols-1 md:grid-cols-5 gap-3 mb-4">
                    <div class="md:col-span-2">
                        <label class="block text-xs font-semibold text-gray-600 mb-1">بحث (اسم / رقم قومي / مرجع)</label>
                        <input id="ircBSearch" oninput="ircApplyBreachFilters()" class="${inp}" placeholder="بحث...">
                    </div>
                    <div>
                        <label class="block text-xs font-semibold text-gray-600 mb-1">المرحلة</label>
                        <select id="ircBStage" onchange="ircApplyBreachFilters()" class="${inp}">
                            <option value="">الكل</option>
                            ${Object.keys(STAGE_LABELS).filter(k => !['closed', 'awaiting_smc', 'data_pending'].includes(k)).map(k => `<option value="${k}">${STAGE_LABELS[k]}</option>`).join('')}
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-semibold text-gray-600 mb-1">الحالة</label>
                        <select id="ircBStatus" onchange="ircApplyBreachFilters()" class="${inp}">
                            <option value="">الكل</option>
                            <option value="open">مستمر</option>
                            <option value="resolved">تم التنفيذ متأخراً</option>
                            <option value="excused">معذور</option>
                            <option value="unexcused">غير معذور</option>
                        </select>
                    </div>
                    <div class="flex items-end">
                        <span class="text-xs text-gray-500" id="ircBCount"></span>
                    </div>
                </div>
                <div id="ircBreachSummary" class="mb-4"></div>
                <div id="ircBreachTable"></div>
                <div id="ircBreachPager" class="flex justify-center items-center gap-3 mt-4 text-sm"></div>
            </div>
        </div>

        <div id="ircModalHost"></div>`;
    }

    /* ─── tabs ──────────────────────────────────────────────────────────────── */
    window.ircSwitchTab = function (tab) {
        _tab = tab;
        const map = { stays: ['ircPanelStays', 'ircTabStays'], breaches: ['ircPanelBreaches', 'ircTabBreaches'] };
        Object.keys(map).forEach(k => {
            const p = document.getElementById(map[k][0]);
            const t = document.getElementById(map[k][1]);
            if (p) p.classList.toggle('hidden', k !== tab);
            if (t) {
                const on = k === tab;
                t.classList.toggle('border-teal-500', on);
                t.classList.toggle('text-teal-700', on);
                t.classList.toggle('bg-teal-50', on);
                t.classList.toggle('border-transparent', !on);
                t.classList.toggle('text-gray-500', !on);
            }
        });
    };

    /* ─── load ──────────────────────────────────────────────────────────────── */
    async function load() {
        if (_loading) return;
        _loading = true;
        const tbl = document.getElementById('ircStaysTable');
        if (tbl && !_rows.length) {
            tbl.innerHTML = '<div class="text-center text-gray-400 py-12"><i class="ri-loader-4-line animate-spin text-2xl block mb-2"></i> جاري التحميل...</div>';
        }
        try {
            const [rows, breaches, runs] = await Promise.all([
                fetchAll('icu_stay_tracking', '*', 'order=check_out_date.desc.nullsfirst'),
                fetchAll('icu_stay_breaches', '*', 'order=due_date.desc'),
                fetchWhere('icu_tracker_runs', 'order=started_at.desc&limit=1'),
            ]);
            _rows = rows;
            _breaches = breaches;
            _lastRun = (runs && runs[0]) || null;
            renderAll();
        } catch (err) {
            console.error('ICU billing load error:', err);
            if (tbl) tbl.innerHTML = `<div class="text-center text-red-500 py-10 text-sm">تعذر التحميل: ${esc(err.message)}<br>تأكد من تشغيل ملف SQL الخاص بالجداول.</div>`;
            toast('❌ تعذر تحميل بيانات متابعة العناية: ' + err.message, 'error');
        } finally {
            _loading = false;
        }
    }
    window.ircReload = function () { load(); };
    window.loadIcuRevenueCycle = function () { load(); };

    function renderAll() {
        renderLastCheck();
        ircApplyFilters();
        ircApplyBreachFilters();
        renderCards();
    }

    function renderLastCheck() {
        const el = document.getElementById('ircLastCheck');
        if (!el) return;
        // newest per-stay check; fall back to the run log
        let latest = null;
        _rows.forEach(r => { if (r.last_checked_at && (!latest || r.last_checked_at > latest)) latest = r.last_checked_at; });
        const ts = latest || (_lastRun && (_lastRun.finished_at || _lastRun.started_at));
        const failed = _rows.filter(r => r.last_check_ok === false).length;
        let html = `آخر فحص: <b>${fmtDateTime(ts)}</b>`;
        if (_lastRun && _lastRun.ok === false) html += ' <span class="text-red-600 font-semibold">- آخر تشغيل فشل</span>';
        if (failed) html += ` <span class="text-amber-600 font-semibold">- ${failed} حالة لم يكتمل فحصها</span>`;
        el.innerHTML = html;
    }

    function renderCards() {
        const today = todayCairo();
        let open = 0, closed = 0, delayed = 0, waived = 0, delayDays = 0;
        _rows.forEach(r => {
            const e = effective(r, today);
            if (e === 'closed') closed++;
            else if (e === 'waived') waived++;
            else { open++; if (e === 'delayed') { delayed++; delayDays += delayOf(r, today); } }
        });
        const card = (n, label, c) => `
            <div class="bg-${c}-50 border border-${c}-200 rounded-xl p-3 text-center">
                <div class="text-2xl font-extrabold text-${c}-700">${n}</div>
                <div class="text-xs text-${c}-600 mt-1">${label}</div>
            </div>`;
        const openBreaches = _breaches.filter(b => b.status === 'open' && !b.excused).length;
        document.getElementById('ircSummaryCards').innerHTML =
            card(open, 'حالات مفتوحة', 'blue') +
            card(delayed, 'حالات متأخرة', 'red') +
            card(delayDays, 'إجمالي أيام التأخير الجارية', 'amber') +
            card(closed + waived, 'مغلقة / مُعفاة', 'emerald') +
            card(openBreaches, 'تأخيرات مسجلة غير معذورة (مستمرة)', 'purple');
    }

    /* ─── stays tab ─────────────────────────────────────────────────────────── */
    window.ircApplyFilters = function () {
        const today = todayCairo();
        const q = (document.getElementById('ircSearch')?.value || '').trim().toLowerCase();
        const st = document.getElementById('ircStatus')?.value || '';
        const sg = document.getElementById('ircStage')?.value || '';
        const from = document.getElementById('ircFrom')?.value || '';
        const to = document.getElementById('ircTo')?.value || '';
        _filtered = _rows.filter(r => {
            if (q && !`${r.patient_name || ''} ${r.patient_nid || ''} ${r.icu_bill_number || ''}`.toLowerCase().includes(q)) return false;
            if (st && effective(r, today) !== st) return false;
            if (sg && r.stage !== sg) return false;
            if (from && (!r.check_out_date || r.check_out_date < from)) return false;
            if (to && (!r.check_out_date || r.check_out_date > to)) return false;
            return true;
        });
        // most delayed first, then oldest due date
        _filtered.sort((a, b) => (delayOf(b, today) - delayOf(a, today)) || String(a.next_due_date || '9999').localeCompare(String(b.next_due_date || '9999')));
        _page = 1;
        renderStays();
    };

    function badge(e) {
        const m = {
            delayed: ['bg-red-100 text-red-700', 'متأخر'],
            open:    ['bg-blue-100 text-blue-700', 'مفتوح'],
            closed:  ['bg-emerald-100 text-emerald-700', 'مغلق'],
            waived:  ['bg-gray-200 text-gray-700', 'مُعفى'],
        }[e];
        return `<span class="px-2 py-0.5 rounded-full text-xs font-semibold ${m[0]}">${m[1]}</span>`;
    }

    function renderStays() {
        const today = todayCairo();
        const host = document.getElementById('ircStaysTable');
        if (!host) return;
        if (!_filtered.length) {
            host.innerHTML = '<div class="text-center text-gray-400 py-12 text-sm">لا توجد حالات مطابقة</div>';
            document.getElementById('ircStaysPager').innerHTML = '';
            return;
        }
        const pages = Math.ceil(_filtered.length / PAGE_SIZE);
        _page = Math.min(_page, pages);
        const slice = _filtered.slice((_page - 1) * PAGE_SIZE, _page * PAGE_SIZE);
        const act = canAct();
        host.innerHTML = `
        <div class="overflow-x-auto">
        <table class="w-full text-sm text-right">
            <thead class="bg-gray-100 text-gray-600 text-xs">
                <tr>
                    <th class="p-2">المريض</th><th class="p-2">الدخول / الخروج</th>
                    <th class="p-2">فاتورة العناية</th><th class="p-2">SMC</th>
                    <th class="p-2">المرحلة</th><th class="p-2">الاستحقاق التالي</th>
                    <th class="p-2">التأخير (أيام)</th>
                    <th class="p-2">قيمة الفاتورة</th><th class="p-2">المغطى بقرارات</th>
                    <th class="p-2">المُفوتر</th><th class="p-2">المتبقي للتغطية</th>
                    <th class="p-2">الحالة</th><th class="p-2">آخر فحص</th><th class="p-2"></th>
                </tr>
            </thead>
            <tbody>
            ${slice.map(r => {
                const e = effective(r, today);
                const d = delayOf(r, today);
                const stageTxt = r.manual_status ? (r.manual_status === 'waived' ? 'مُعفى يدوياً' : 'مغلق يدوياً') : (STAGE_LABELS[r.stage] || r.stage || '—');
                const failNote = r.last_check_ok === false ? `<div class="text-xs text-amber-600" title="${esc(r.last_check_note)}">⚠ فحص ناقص</div>` : '';
                return `
                <tr class="border-b hover:bg-gray-50 ${e === 'delayed' ? 'bg-red-50/40' : ''}">
                    <td class="p-2"><div class="font-semibold">${esc(r.patient_name || '—')}</div><div class="text-xs text-gray-500">${esc(r.patient_nid || '')}</div></td>
                    <td class="p-2 whitespace-nowrap">${fmtDate(r.check_in_date)}<br>${fmtDate(r.check_out_date)}</td>
                    <td class="p-2">${r.icu_bill_number ? `${esc(r.icu_bill_number)}<div class="text-xs text-gray-500">${fmtDate(r.bill_created_on)}</div>` : '<span class="text-red-600 text-xs">لا توجد</span>'}</td>
                    <td class="p-2 text-xs">${esc(SMC_LABELS[r.smc_summary] || '—')}<div class="text-gray-500">${r.requests_count || 0} طلب</div></td>
                    <td class="p-2 text-xs">${esc(stageTxt)}${failNote}</td>
                    <td class="p-2 whitespace-nowrap text-xs">${r.next_due_date ? `${fmtDate(r.next_due_date)}<div class="text-gray-500">${esc(STAGE_LABELS[r.next_due_stage] || '')}</div>` : '—'}</td>
                    <td class="p-2 font-bold ${d ? 'text-red-600' : 'text-gray-400'}">${d || '—'}</td>
                    <td class="p-2 whitespace-nowrap">${money(r.bill_total)}</td>
                    <td class="p-2 whitespace-nowrap">${money(r.covered_value)}</td>
                    <td class="p-2 whitespace-nowrap">${money(r.billed_value)}</td>
                    <td class="p-2 whitespace-nowrap ${Number(r.remaining_to_cover) > 0 ? 'text-amber-700 font-semibold' : ''}">${money(r.remaining_to_cover)}</td>
                    <td class="p-2">${badge(e)}${r.manual_reason ? `<div class="text-xs text-gray-500 max-w-[10rem]" title="${esc(r.manual_reason)}">${esc(r.manual_by || '')}</div>` : ''}</td>
                    <td class="p-2 whitespace-nowrap text-xs text-gray-500">${fmtDateTime(r.last_checked_at)}</td>
                    <td class="p-2 whitespace-nowrap">
                        <button onclick="ircDetails(${Number(r.admission_id)})" class="text-teal-700 hover:underline text-xs">تفاصيل</button>
                        ${act ? `<button onclick="ircOverride(${Number(r.admission_id)})" class="text-gray-600 hover:underline text-xs mr-2">إغلاق/إعفاء</button>` : ''}
                    </td>
                </tr>`;
            }).join('')}
            </tbody>
        </table>
        </div>`;
        document.getElementById('ircStaysPager').innerHTML = pagerHTML(_page, pages, 'ircPage', _filtered.length);
    }

    function pagerHTML(page, pages, fn, total) {
        if (pages <= 1) return `<span class="text-gray-500">${total} حالة</span>`;
        return `
            <button ${page <= 1 ? 'disabled' : ''} onclick="${fn}(-1)" class="px-3 py-1 border rounded disabled:opacity-40">السابق</button>
            <span>${page} / ${pages} <span class="text-gray-500">(${total})</span></span>
            <button ${page >= pages ? 'disabled' : ''} onclick="${fn}(1)" class="px-3 py-1 border rounded disabled:opacity-40">التالي</button>`;
    }
    window.ircPage = function (d) { _page += d; renderStays(); };
    window.ircBPage = function (d) { _bPage += d; renderBreaches(); };

    /* ─── detail drawer ─────────────────────────────────────────────────────── */
    function modal(html) {
        const host = document.getElementById('ircModalHost');
        host.innerHTML = `
        <div class="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onclick="if(event.target===this)ircCloseModal()">
            <div class="bg-white rounded-2xl shadow-xl w-full max-w-4xl max-h-[90vh] overflow-y-auto p-6">${html}</div>
        </div>`;
    }
    window.ircCloseModal = function () { const h = document.getElementById('ircModalHost'); if (h) h.innerHTML = ''; };

    window.ircDetails = async function (admId) {
        const r = _rows.find(x => x.admission_id === admId);
        if (!r) return;
        modal('<div class="text-center text-gray-400 py-10"><i class="ri-loader-4-line animate-spin text-2xl"></i></div>');
        try {
            const [reqs, decs, recs] = await Promise.all([
                fetchWhere('icu_stay_requests', `admission_id=eq.${admId}`, 'request_date.asc'),
                fetchWhere('icu_stay_decrees', `admission_id=eq.${admId}`, 'decree_date.asc'),
                fetchWhere('icu_stay_receipts', `admission_id=eq.${admId}`, 'registered_on.asc'),
            ]);
            const bs = _breaches.filter(b => b.admission_id === admId);
            const th = 'p-2 bg-gray-100 text-xs text-gray-600';
            const t = (heads, rows) => rows.length
                ? `<div class="overflow-x-auto"><table class="w-full text-xs text-right"><thead><tr>${heads.map(h => `<th class="${th}">${h}</th>`).join('')}</tr></thead><tbody>${rows.map(c => `<tr class="border-b">${c.map(x => `<td class="p-2">${x}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`
                : '<div class="text-xs text-gray-400 py-2">لا يوجد</div>';
            modal(`
                <div class="flex justify-between items-start mb-4">
                    <div>
                        <h3 class="text-lg font-bold">${esc(r.patient_name || '')}</h3>
                        <div class="text-xs text-gray-500">${esc(r.patient_nid || '')} · دخول ${fmtDate(r.check_in_date)} · خروج ${fmtDate(r.check_out_date)}</div>
                    </div>
                    <button onclick="ircCloseModal()" class="text-gray-400 hover:text-gray-700 text-xl"><i class="ri-close-line"></i></button>
                </div>
                ${r.last_check_note ? `<div class="bg-amber-50 border border-amber-200 rounded-lg p-2 text-xs mb-3">${esc(r.last_check_note)}</div>` : ''}
                <h4 class="font-bold text-sm mt-3 mb-1">طلبات القرار على SMC</h4>
                ${t(['رقم الطلب', 'التاريخ', 'الحالة', 'ملاحظة', 'رقم القرار', 'خطاب'],
                    reqs.map(q => [esc(q.request_number), fmtDate(q.request_date), esc(q.status_text || ''),
                        q.is_icu === false ? 'غير عناية' : (q.is_icu ? 'إثبات عناية' : '—'),
                        esc(q.decree_number || '—'), q.letter_date ? fmtDate(q.letter_date) : '—']))}
                <h4 class="font-bold text-sm mt-4 mb-1">القرارات الصادرة</h4>
                ${t(['رقم القرار', 'التاريخ', 'القيمة', 'المُفوتر', 'عدد الفواتير'],
                    decs.map(d => [esc(d.decree_number), fmtDate(d.decree_date), money(d.decree_value), money(d.receipts_total), d.receipts_count || 0]))}
                <h4 class="font-bold text-sm mt-4 mb-1">الفواتير على SMC (تاريخ التسجيل)</h4>
                ${t(['القرار', 'تاريخ التسجيل', 'القيمة', 'الحالة', 'محتسبة'],
                    recs.map(c => [esc(c.decree_number), fmtDate(c.registered_on), money(c.value), esc(c.status_text || c.status_code || ''), c.counted ? 'نعم' : 'لا (مرفوضة)']))}
                <h4 class="font-bold text-sm mt-4 mb-1">التأخيرات المسجلة</h4>
                ${t(['المرحلة', 'المرجع', 'الاستحقاق', 'تم في', 'أيام التأخير', 'الحالة', 'العذر'],
                    bs.map(b => [esc(STAGE_LABELS[b.stage] || b.stage), esc(b.ref || '—'), fmtDate(b.due_date), fmtDate(b.done_on),
                        `<b class="text-red-600">${b.delay_days}</b>`, b.status === 'open' ? 'مستمر' : 'منتهٍ',
                        b.excused ? esc(b.excuse_reason || 'معذور') : '—']))}
                ${r.manual_status ? `<div class="mt-4 text-xs bg-gray-50 border rounded-lg p-2">تعديل يدوي: <b>${r.manual_status === 'waived' ? 'إعفاء' : 'إغلاق'}</b> بواسطة ${esc(r.manual_by || '')} في ${fmtDateTime(r.manual_at)} - ${esc(r.manual_reason || '')}</div>` : ''}
            `);
        } catch (err) {
            modal(`<div class="text-red-500 text-sm">${esc(err.message)}</div><button onclick="ircCloseModal()" class="mt-3 px-3 py-1 border rounded">إغلاق</button>`);
        }
    };

    /* ─── manual close / waive ──────────────────────────────────────────────── */
    window.ircOverride = function (admId) {
        const r = _rows.find(x => x.admission_id === admId);
        if (!r || !canAct()) return;
        modal(`
            <h3 class="text-lg font-bold mb-1">إغلاق / إعفاء حالة</h3>
            <p class="text-xs text-gray-500 mb-4">${esc(r.patient_name || '')} - ${esc(r.patient_nid || '')}. التأخيرات المسجلة سابقاً تبقى في السجل ولا تتغير.</p>
            <label class="block text-xs font-semibold mb-1">الإجراء</label>
            <select id="ircOvStatus" class="w-full p-2 border rounded-lg text-sm mb-3">
                <option value="closed">إغلاق (تمت المعالجة خارج النظام)</option>
                <option value="waived">إعفاء (لا تتم المتابعة)</option>
                ${r.manual_status ? '<option value="">إلغاء التعديل اليدوي</option>' : ''}
            </select>
            <label class="block text-xs font-semibold mb-1">السبب (إلزامي)</label>
            <textarea id="ircOvReason" rows="3" class="w-full p-2 border rounded-lg text-sm mb-4">${esc(r.manual_reason || '')}</textarea>
            <div class="flex gap-2 justify-end">
                <button onclick="ircCloseModal()" class="px-4 py-2 border rounded-lg text-sm">إلغاء</button>
                <button onclick="ircSaveOverride(${Number(admId)})" class="px-4 py-2 bg-teal-600 text-white rounded-lg text-sm font-semibold">حفظ</button>
            </div>`);
    };
    window.ircSaveOverride = async function (admId) {
        const status = document.getElementById('ircOvStatus').value;
        const reason = document.getElementById('ircOvReason').value.trim();
        if (status && !reason) { toast('يرجى كتابة السبب', 'error'); return; }
        const body = status
            ? { manual_status: status, manual_reason: reason, manual_by: currentUserName(), manual_at: new Date().toISOString() }
            : { manual_status: null, manual_reason: null, manual_by: null, manual_at: null };
        try {
            await patchRow('icu_stay_tracking', `admission_id=eq.${admId}`, body);
            const r = _rows.find(x => x.admission_id === admId);
            if (r) Object.assign(r, body);
            ircCloseModal();
            renderCards();
            ircApplyFilters();
            toast('✅ تم الحفظ', 'success');
        } catch (err) {
            toast('❌ تعذر الحفظ: ' + err.message, 'error');
        }
    };

    /* ─── breaches tab ──────────────────────────────────────────────────────── */
    function stayOf(id) { return _rows.find(r => r.admission_id === id) || {}; }

    window.ircApplyBreachFilters = function () {
        const q = (document.getElementById('ircBSearch')?.value || '').trim().toLowerCase();
        const sg = document.getElementById('ircBStage')?.value || '';
        const st = document.getElementById('ircBStatus')?.value || '';
        _breachFiltered = _breaches.filter(b => {
            const s = stayOf(b.admission_id);
            if (q && !`${s.patient_name || ''} ${s.patient_nid || ''} ${b.ref || ''}`.toLowerCase().includes(q)) return false;
            if (sg && b.stage !== sg) return false;
            if (st === 'open' && b.status !== 'open') return false;
            if (st === 'resolved' && b.status !== 'resolved') return false;
            if (st === 'excused' && !b.excused) return false;
            if (st === 'unexcused' && b.excused) return false;
            return true;
        });
        _bPage = 1;
        renderBreaches();
    };

    function renderBreaches() {
        const host = document.getElementById('ircBreachTable');
        if (!host) return;
        // adherence summary per stage (unexcused only count against the user)
        const per = {};
        _breaches.forEach(b => {
            const o = per[b.stage] || (per[b.stage] = { n: 0, ex: 0, days: 0 });
            o.n++; if (b.excused) o.ex++; else o.days += b.delay_days || 0;
        });
        document.getElementById('ircBreachSummary').innerHTML = Object.keys(per).length
            ? `<div class="grid grid-cols-2 md:grid-cols-4 gap-2">${Object.keys(per).map(k => `
                <div class="border rounded-lg p-2 text-xs">
                    <div class="font-semibold">${esc(STAGE_LABELS[k] || k)}</div>
                    <div class="text-gray-600">${per[k].n} تأخير · ${per[k].ex} معذور</div>
                    <div class="text-red-600 font-bold">${per[k].days} يوم (غير معذور)</div>
                </div>`).join('')}</div>` : '';
        document.getElementById('ircBCount').textContent = `${_breachFiltered.length} سجل`;
        if (!_breachFiltered.length) {
            host.innerHTML = '<div class="text-center text-gray-400 py-12 text-sm">لا توجد تأخيرات مسجلة</div>';
            document.getElementById('ircBreachPager').innerHTML = '';
            return;
        }
        const pages = Math.ceil(_breachFiltered.length / PAGE_SIZE);
        _bPage = Math.max(1, Math.min(_bPage, pages));
        const slice = _breachFiltered.slice((_bPage - 1) * PAGE_SIZE, _bPage * PAGE_SIZE);
        const act = canAct();
        host.innerHTML = `
        <div class="overflow-x-auto">
        <table class="w-full text-sm text-right">
            <thead class="bg-gray-100 text-gray-600 text-xs"><tr>
                <th class="p-2">المريض</th><th class="p-2">المرحلة</th><th class="p-2">المرجع</th>
                <th class="p-2">الاستحقاق</th><th class="p-2">تم في</th><th class="p-2">أيام التأخير</th>
                <th class="p-2">الحالة</th><th class="p-2">اكتُشف</th><th class="p-2">العذر</th><th class="p-2"></th>
            </tr></thead>
            <tbody>
            ${slice.map(b => {
                const s = stayOf(b.admission_id);
                return `<tr class="border-b hover:bg-gray-50">
                    <td class="p-2"><div class="font-semibold">${esc(s.patient_name || '—')}</div><div class="text-xs text-gray-500">${esc(s.patient_nid || '')}</div></td>
                    <td class="p-2 text-xs">${esc(STAGE_LABELS[b.stage] || b.stage)}</td>
                    <td class="p-2 text-xs">${esc(b.ref || '—')}</td>
                    <td class="p-2 whitespace-nowrap">${fmtDate(b.due_date)}</td>
                    <td class="p-2 whitespace-nowrap">${fmtDate(b.done_on)}</td>
                    <td class="p-2 font-bold ${b.excused ? 'text-gray-400 line-through' : 'text-red-600'}">${b.delay_days}</td>
                    <td class="p-2 text-xs">${b.status === 'open' ? '<span class="text-red-600 font-semibold">مستمر</span>' : 'تم متأخراً'}</td>
                    <td class="p-2 text-xs whitespace-nowrap">${fmtDateTime(b.first_detected_at)}</td>
                    <td class="p-2 text-xs max-w-[14rem]">${b.excused ? `<span class="text-emerald-700 font-semibold">معذور</span> - ${esc(b.excuse_reason || '')}<div class="text-gray-500">${esc(b.excused_by || '')} ${fmtDateTime(b.excused_at)}</div>` : '—'}</td>
                    <td class="p-2 whitespace-nowrap">${act ? `<button onclick="ircExcuse(${Number(b.id)})" class="text-teal-700 hover:underline text-xs">${b.excused ? 'تعديل العذر' : 'قبول عذر'}</button>` : ''}</td>
                </tr>`;
            }).join('')}
            </tbody>
        </table></div>`;
        document.getElementById('ircBreachPager').innerHTML = pagerHTML(_bPage, pages, 'ircBPage', _breachFiltered.length);
    }

    window.ircExcuse = function (id) {
        const b = _breaches.find(x => x.id === id);
        if (!b || !canAct()) return;
        modal(`
            <h3 class="text-lg font-bold mb-1">قبول عذر عن تأخير</h3>
            <p class="text-xs text-gray-500 mb-4">${esc(STAGE_LABELS[b.stage] || b.stage)} - ${b.delay_days} يوم. عدد أيام التأخير الأصلي يبقى مسجلاً كما هو.</p>
            <label class="block text-xs font-semibold mb-1">الإجراء</label>
            <select id="ircExStatus" class="w-full p-2 border rounded-lg text-sm mb-3">
                <option value="1">قبول العذر</option>
                ${b.excused ? '<option value="0">إلغاء العذر</option>' : ''}
            </select>
            <label class="block text-xs font-semibold mb-1">سبب العذر (إلزامي عند القبول)</label>
            <textarea id="ircExReason" rows="3" class="w-full p-2 border rounded-lg text-sm mb-4">${esc(b.excuse_reason || '')}</textarea>
            <div class="flex gap-2 justify-end">
                <button onclick="ircCloseModal()" class="px-4 py-2 border rounded-lg text-sm">إلغاء</button>
                <button onclick="ircSaveExcuse(${Number(id)})" class="px-4 py-2 bg-teal-600 text-white rounded-lg text-sm font-semibold">حفظ</button>
            </div>`);
    };
    window.ircSaveExcuse = async function (id) {
        const on = document.getElementById('ircExStatus').value === '1';
        const reason = document.getElementById('ircExReason').value.trim();
        if (on && !reason) { toast('يرجى كتابة سبب العذر', 'error'); return; }
        const body = on
            ? { excused: true, excuse_reason: reason, excused_by: currentUserName(), excused_at: new Date().toISOString() }
            : { excused: false, excuse_reason: null, excused_by: null, excused_at: null };
        try {
            await patchRow('icu_stay_breaches', `id=eq.${id}`, body);
            const b = _breaches.find(x => x.id === id);
            if (b) Object.assign(b, body);
            ircCloseModal();
            renderCards();
            ircApplyBreachFilters();
            toast('✅ تم الحفظ', 'success');
        } catch (err) {
            toast('❌ تعذر الحفظ: ' + err.message, 'error');
        }
    };

    /* ─── Excel export (exports exactly what the active tab shows) ──────────── */
    function ensureXLSX() {
        if (window.XLSX) return Promise.resolve();
        return new Promise((resolve, reject) => {
            const existing = document.getElementById('xlsxCdnScript');
            if (existing) { existing.addEventListener('load', resolve); existing.addEventListener('error', reject); return; }
            const s = document.createElement('script');
            s.id = 'xlsxCdnScript';
            s.src = 'https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js';
            s.onload = resolve;
            s.onerror = () => reject(new Error('فشل تحميل مكتبة Excel'));
            document.head.appendChild(s);
        });
    }
    window.ircExport = async function () {
        try {
            await ensureXLSX();
            const today = todayCairo();
            let rows, name;
            if (_tab === 'breaches') {
                name = 'سجل_تأخيرات_العناية';
                rows = _breachFiltered.map(b => {
                    const s = stayOf(b.admission_id);
                    return {
                        'المريض': s.patient_name || '', 'الرقم القومي': s.patient_nid || '',
                        'المرحلة': STAGE_LABELS[b.stage] || b.stage, 'المرجع': b.ref || '',
                        'تاريخ الاستحقاق': fmtDate(b.due_date), 'تم في': fmtDate(b.done_on),
                        'أيام التأخير': b.delay_days, 'الحالة': b.status === 'open' ? 'مستمر' : 'تم متأخراً',
                        'معذور': b.excused ? 'نعم' : 'لا', 'سبب العذر': b.excuse_reason || '',
                        'اكتُشف': fmtDateTime(b.first_detected_at),
                    };
                });
            } else {
                name = 'متابعة_فوترة_العناية';
                rows = _filtered.map(r => {
                    const e = effective(r, today);
                    return {
                        'المريض': r.patient_name || '', 'الرقم القومي': r.patient_nid || '',
                        'تاريخ الدخول': fmtDate(r.check_in_date), 'تاريخ الخروج': fmtDate(r.check_out_date),
                        'رقم الفاتورة': r.icu_bill_number || '', 'تاريخ الفاتورة': fmtDate(r.bill_created_on),
                        'حالة SMC': SMC_LABELS[r.smc_summary] || '', 'عدد الطلبات': r.requests_count || 0,
                        'المرحلة': r.manual_status ? 'تعديل يدوي' : (STAGE_LABELS[r.stage] || r.stage || ''),
                        'الاستحقاق التالي': fmtDate(r.next_due_date), 'نوع الاستحقاق': STAGE_LABELS[r.next_due_stage] || '',
                        'أيام التأخير': delayOf(r, today),
                        'قيمة الفاتورة': r.bill_total == null ? '' : Number(r.bill_total),
                        'المغطى بقرارات': r.covered_value == null ? '' : Number(r.covered_value),
                        'المُفوتر على SMC': r.billed_value == null ? '' : Number(r.billed_value),
                        'المتبقي للتغطية': r.remaining_to_cover == null ? '' : Number(r.remaining_to_cover),
                        'الحالة': { delayed: 'متأخر', open: 'مفتوح', closed: 'مغلق', waived: 'مُعفى' }[e],
                        'سبب التعديل اليدوي': r.manual_reason || '', 'آخر فحص': fmtDateTime(r.last_checked_at),
                    };
                });
            }
            if (!rows.length) { toast('لا توجد بيانات للتصدير', 'error'); return; }
            const wb = XLSX.utils.book_new();
            XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(rows), 'Sheet1');
            XLSX.writeFile(wb, `${name}_${today}.xlsx`);
        } catch (err) {
            toast('❌ فشل التصدير: ' + err.message, 'error');
        }
    };

    /* ─── init ──────────────────────────────────────────────────────────────── */
    function init() {
        try {
            if (!createSection()) {
                console.warn('⚠️ ICU Billing Workflow: main container not found, retrying...');
                setTimeout(init, 1000);
                return;
            }
            window.icuRevenueCycleReady = true;
            console.log('✅ ICU Billing Workflow Module ready');
        } catch (err) {
            console.error('❌ ICU Billing Workflow Module failed:', err);
            window.icuRevenueCycleReady = false;
        }
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
