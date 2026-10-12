/* gitpro.dev script: theme toggle, copy buttons and animated figures. Engine shared with yantrakar.com. Vanilla JS. */
(() => {
	const d = document, root = d.documentElement;
	const tb = d.querySelector('.theme-toggle');
	if (tb) {
		const mq = matchMedia('(prefers-color-scheme: light)');
		const next = () => (root.getAttribute('data-theme') || (mq.matches ? 'light' : 'dark')) === 'dark' ? 'light' : 'dark';
		const paint = () => { const n = next(); tb.querySelector('span').textContent = n === 'light' ? 'Switch to light' : 'Switch to dark'; tb.setAttribute('aria-label', 'Switch to ' + n + ' theme'); };
		tb.hidden = false; paint();
		tb.onclick = () => {
			const n = next();
			root.setAttribute('data-theme', n);
			try { localStorage.setItem('yk-theme', n); } catch (e) { }
			paint();
		};
		mq.addEventListener('change', paint);
	}
	/* copy buttons: hidden without JS, since they need it */
	d.querySelectorAll('button[data-copy]').forEach(b => {
		if (!navigator.clipboard) return;
		b.hidden = false;
		b.onclick = () => navigator.clipboard.writeText(b.dataset.copy).then(() => {
			b.textContent = 'Copied'; setTimeout(() => { b.textContent = 'Copy'; }, 1500);
		}, () => { });
	});
	if (matchMedia('(prefers-reduced-motion: reduce)').matches || !window.IntersectionObserver) return;
	const SPD = [0.18, 0.5, 1.5], BLD = {}, figs = [];
	let raf = 0, last = 0;
	const at = (e, a) => { for (const k in a) e.setAttribute(k, a[k]); return e; };
	const mk = (n, a, p) => (p || { appendChild: e => e }).appendChild(at(d.createElementNS('http://www.w3.org/2000/svg', n), a));
	function G(S, k) {
		const e = S.q(k), a = n => +e.getAttribute(n);
		if (e.tagName == 'line') return { x: a('x1'), y: a('y1'), y2: a('y2') };
		if (e.tagName == 'g') return { x: +e.firstChild.getAttribute('x1') };
		const x = a('x'), y = a('y'), w = a('width'), h = a('height');
		return { x, y, w, cx: x + w / 2, cy: y + h / 2, r: x + w, b: y + h };
	}
	const txt = (S, k, x, y, cls, anchor) => { S.c[k] = mk('text', { class: cls, x, y, 'text-anchor': anchor || 'start', 'data-k': k }, S.u); };
	function TL(S) {
		const T = { ev: [], tw: [], end: 0 };
		T.at = (t, f) => { T.ev.push([t, f]); if (t > T.end) T.end = t; };
		T.cl = (t, k, c, on) => T.at(t, () => { const e = S.q(k); if (e) e.classList.toggle(c, on); });
		T.hit = (t, k) => T.cl(t, k, k == 'x-mark' ? 'flash' : 'hit', 1);
		T.act = (t, k, ms) => { T.cl(t, k, 'active', 1); T.cl(t + ms, k, 'active', 0); };
		T.tx = (t, k, s) => T.at(t, () => { const e = S.q(k); if (e) e.textContent = s; });
		T.pk = (t, pts, o = {}) => {
			let segs = [], p = pts[0], s0 = t;
			let u = o.m ? S.mkr : S.pool.find(c => c.until <= t);
			if (!u) { u = { el: mk('circle', {}, S.g) }; if (o.m) S.mkr = u; else S.pool.push(u); }
			pts.forEach((q, i) => {
				if (!i) return;
				const dx = q[0] - p[0], dy = q[1] - p[1];
				segs.push({ x: p[0], y: p[1], dx, dy, d: Math.hypot(dx, dy) / SPD[q[3] || 0], h: q[3] == 2 });
				p = q;
				if (q[2] || i == pts.length - 1) {
					const sum = segs.reduce((s, g) => s + g.d, 0), D = Math.max(sum, 260);
					segs.forEach(g => { g.d = sum ? g.d * D / sum : 0; });
					T.tw.push({ el: u.el, s: s0, d: D, segs, keep: o.m || i < pts.length - 1, cl: o.m ? 'mk' : 'pkt' + (o.a ? ' a' : ''), r: o.m ? 7 : o.r || 4 });
					s0 += D; segs = [];
					if (q[2]) T.hit(s0, q[2]);
				}
			});
			u.until = s0; if (s0 > T.end) T.end = s0;
			return s0;
		};
		return T;
	}
	function place(w, t) {
		let e = t - w.s, i = 0, k;
		if (e < 0 || w.fin) return;
		if (e > w.d) { w.fin = 1; e = w.d; }
		if (!w.on) at(w.el, { class: w.cl, r: w.r });
		w.on = 1; k = (e = e / w.d) * e * (3 - 2 * e) * w.d;
		for (; i < w.segs.length - 1 && k > w.segs[i].d; i++) k -= w.segs[i].d;
		const s = w.segs[i], g = s.d ? k / s.d : 1;
		at(w.el, { cx: (s.x + s.dx * g).toFixed(1), cy: (s.y + s.dy * g).toFixed(1) });
		w.el.style.transitionDuration = s.h ? '0s' : '';
		w.el.style.opacity = s.h || (w.fin && !w.keep) ? 0 : 1;
	}
	function run(S, t) {
		const tl = S.tl;
		while (S.i < tl.ev.length && tl.ev[S.i][0] <= t) tl.ev[S.i++][1]();
		tl.tw.forEach(w => place(w, t));
	}
	function reset(S) {
		S.svg.querySelectorAll('.hit,.active,.flash,.on').forEach(e => e.classList.remove('hit', 'active', 'flash', 'on'));
		S.i = 0;
		S.pool.forEach(p => { p.until = 0; });
		const T = TL(S);
		S.build(S, T, S.n, k => G(S, k));
		T.ev.sort((a, b) => a[0] - b[0]); S.tl = T;
	}
	/* routes come from figures.py; chain() joins them with hidden jumps, so packets never cross a box */
	const route = (S, k) => (S.R || (S.R = JSON.parse(S.svg.dataset.routes)))[k];
	const chain = (S, legs) => {
		const out = [];
		legs.forEach(([k, hit], i) => {
			route(S, k).forEach((p, j) => out.push(!j && i ? [p[0], p[1], 0, 2] : [p[0], p[1]]));
			out[out.length - 1][2] = hit || 0;
		});
		return out;
	};
	const hold = (T, t) => T.at(t, () => { });
	BLD.f01 = (S, T) => {
		['m1-v', 'm2-v'].forEach(k => { T.tx(0, k, 'no commits yet'); T.cl(0, k, 'val', 0); });
		T.tx(0, 'st', 'PERSONAL REPO'); T.act(0, 'gc', 500);
		let t = T.pk(300, chain(S, [['c-r2', 'r2']])) + 150;
		t = T.pk(t, chain(S, [['p-m2', 'm2']])); T.tx(t, 'm2-v', 'jane@personal.dev');
		t += 900; T.tx(t, 'st', 'WORK REPO, SAME EMAIL'); T.act(t, 'gc', 500);
		t = T.pk(t + 300, chain(S, [['c-r1', 'r1']])) + 150;
		t = T.pk(t, chain(S, [['p-m1', 'm1']]), { a: 1 });
		T.tx(t, 'm1-v', 'jane@personal.dev'); T.cl(t, 'm1-v', 'val', 1);
		T.tx(t, 'st', 'PERSONAL EMAIL IN WORK HISTORY'); T.act(t, 'm1', 2600);
	};
	BLD.f02 = (S, T) => {
		const W = S.svg.dataset.v == 'w';
		const st = ['MATCH: github.com / employer-org', 'MATCH: gitlab.employer.com', 'NO MATCH: default identity'];
		let t = 200;
		for (let i = 1; i < 4; i++) {
			T.tx(t, 'st', st[i - 1]); T.hit(t, 'r' + i);
			t = T.pk(t + 150, chain(S, W ? [['a' + i, 'p' + i], ['b' + i, 'id' + i]] : [['a' + i, 'id' + i]]));
			T.act(t, 'id' + i, 700); t += 700;
		}
		T.tx(t, 'st', 'OVERRIDE IN .git/config');
		t = T.pk(t, chain(S, [['c-ov', 'ov']])) + 400;
		t = T.pk(t, chain(S, [['c-g', 'guard'], ['c-x', 'x-mark']]), { a: 1 });
		T.tx(t, 'st', 'COMMIT BLOCKED, FIX SHOWN'); T.act(t, 'guard', 2400);
	};
	BLD.f03 = (S, T) => {
		T.tx(0, 'st', 'AGENT RUNS git commit'); T.hit(0, 'agent');
		let t = T.pk(200, chain(S, [['q1', 'check']]), { a: 1 });
		T.tx(t, 'st', 'WRONG IDENTITY'); T.act(t, 'check', 700);
		t = T.pk(t + 300, chain(S, [['g-stop', 'x-mark']]), { a: 1 }) + 200;
		T.tx(t, 'st', 'DENIED: ASK THE USER');
		t = T.pk(t, chain(S, [['q2', 'agent']])) + 300;
		T.tx(t, 'st', 'AGENT ASKS YOU');
		t = T.pk(t, chain(S, [['u1', 'you']]), { a: 1 });
		T.act(t, 'you', 900); t += 900;
		T.tx(t, 'st', 'YOU APPROVE THE FIX');
		t = T.pk(t, chain(S, [['u2', 'agent']])) + 300;
		T.tx(t, 'st', 'AGENT RUNS git commit');
		t = T.pk(t, chain(S, [['q1', 'check'], ['g1', 'git']]), { a: 1 });
		T.tx(t, 'st', 'COMMITS AS work-github'); T.act(t, 'git', 2400);
	};
	BLD.f04 = (S, T) => {
		const st = ['SAVED, NOTHING APPLIED YET', 'PLAN SHOWS EVERY CHANGE', 'APPLY WRITES PLAIN CONFIG'];
		T.tx(0, 'st', 'ANSWER A FEW QUESTIONS'); T.act(0, 's1', 1000);
		let t = 1000;
		for (let i = 1; i < 4; i++) {
			t = T.pk(t, chain(S, [['k' + i, 's' + (i + 1)]]));
			T.tx(t, 'st', st[i - 1]); T.act(t, 's' + (i + 1), 1000); t += 1000;
		}
		const e = Math.max(...[1, 2, 3].map(i => T.pk(t - 400, chain(S, [['d' + i, 'o' + i]])))) + 400;
		T.tx(e, 'st', 'GIT DOES THE MATCHING');
		t = T.pk(e, chain(S, [['w', 'w0']])); T.act(t, 'w0', 1200);
		T.tx(t + 1400, 'st', 'UNDO: gitpro uninstall'); hold(T, t + 3400);
	};
	const start = f => { f.t = f.len = 0; f.s.forEach(S => { S.n = f.n; reset(S); f.len = Math.max(f.len, S.tl.end); }); };
	const on = f => f.vis && !f.pa;
	function tick(now) {
		const dt = Math.min(now - last, 80); last = now; raf = 0;
		figs.filter(on).forEach(f => {
			f.t += dt; f.s.forEach(S => run(S, f.t));
			if (f.t > f.len + 1200) { f.n++; start(f); }
		});
		if (figs.some(on)) raf = requestAnimationFrame(tick); /* browsers pause rAF while the tab is hidden */
	}
	const wake = () => { if (!raf) { last = performance.now(); raf = requestAnimationFrame(tick); } };
	const io = new IntersectionObserver(es => es.forEach(x => {
		const f = x.target._f; f.vis = x.intersectionRatio > 0.34;
		if (f.vis && !f.go) { f.go = 1; start(f); }
		if (f.vis) wake();
	}), { threshold: [0, 0.35] });
	d.querySelectorAll('figure[data-anim]').forEach(el => {
		const f = { el, s: [], n: 0, vis: 0, pa: 0 };
		el.querySelectorAll('svg[data-fig]').forEach(svg => {
			const S = { svg, c: {}, build: BLD[svg.dataset.fig], pool: [], n: 0, i: 0 };
			svg.onanimationend = e => e.target.classList.remove('hit', 'flash');
			S.u = mk('g', { 'aria-hidden': 'true' }, svg); S.g = mk('g', { class: 'pkts' }, S.u);
			S.q = k => S.c[k] || (S.c[k] = svg.querySelector('[data-k="' + k + '"]'));
			if (S.build) f.s.push(S);
		});
		if (!f.s.length) return;
		el.classList.add('anim'); el._f = f;
		const b = d.createElement('button');
		b.type = 'button'; b.className = 'fig-ctl'; b.textContent = 'Pause'; b.setAttribute('aria-pressed', 'false');
		b.onclick = () => { f.pa = !f.pa; b.textContent = f.pa ? 'Play' : 'Pause'; b.setAttribute('aria-pressed', f.pa); wake(); };
		el.querySelector('.fig-head').appendChild(b);
		figs.push(f); io.observe(el);
	});
})();
