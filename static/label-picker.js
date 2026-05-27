/**
 * LabelPicker — searchable multi/single-select label dropdown.
 *
 * Usage:
 *   const picker = new LabelPicker(containerEl, {
 *     allLabels: [{id, name}],
 *     initialIds: [1, 3],
 *     mode: 'multi',          // 'multi' | 'single'
 *     allowCreate: true,
 *     placeholder: 'Add label…',
 *     onChange(selectedSet) {},          // fired on every change
 *     onLabelsRefreshed(labels) {},      // fired after a new label is created
 *   });
 *   picker.getSelected();   // → Set<id>
 *   picker.setSelected([1, 2]);
 *   picker.setLabels([...]);
 */
class LabelPicker {
  constructor(el, opts = {}) {
    this.el = el;
    this.mode = opts.mode || 'multi';
    this.allowCreate = opts.allowCreate !== false;
    this.placeholder = opts.placeholder || 'Search labels…';
    this.onChange = opts.onChange || null;
    this.onLabelsRefreshed = opts.onLabelsRefreshed || null;
    this.allLabels = [...(opts.allLabels || [])];
    this.selected = new Set(opts.initialIds || []);
    this._q = '';
    this._open = false;
    this._build();
  }

  setLabels(labels) {
    this.allLabels = [...labels];
    this._renderChips();
    if (this._open) this._renderList();
  }

  getSelected() { return new Set(this.selected); }

  setSelected(ids) {
    this.selected = new Set(ids);
    this._renderChips();
    if (this._open) this._renderList();
  }

  // ── Internal ──────────────────────────────────────────────────────────────

  _build() {
    this.el.classList.add('lp-wrap');
    this.el.innerHTML = '';

    this._chips = document.createElement('div');
    this._chips.className = 'lp-chips';

    this._inputRow = document.createElement('div');
    this._inputRow.className = 'lp-input-row';

    this._input = document.createElement('input');
    this._input.type = 'text';
    this._input.className = 'lp-search';
    this._input.placeholder = this.placeholder;
    this._input.setAttribute('autocomplete', 'off');
    this._input.setAttribute('autocorrect', 'off');
    this._input.setAttribute('spellcheck', 'false');

    this._list = document.createElement('div');
    this._list.className = 'lp-dropdown';

    this._inputRow.appendChild(this._input);
    this._inputRow.appendChild(this._list);
    this.el.appendChild(this._chips);
    this.el.appendChild(this._inputRow);

    this._input.addEventListener('focus', () => {
      this._open = true;
      this._renderList();
      this._list.style.display = '';
    });

    this._input.addEventListener('blur', () => {
      setTimeout(() => {
        if (!this._keepOpen) {
          this._open = false;
          this._list.style.display = 'none';
          this._input.value = '';
          this._q = '';
          this._renderList();
        }
        this._keepOpen = false;
      }, 200);
    });

    this._input.addEventListener('input', () => {
      this._q = this._input.value;
      this._renderList();
    });

    this._renderChips();
    this._list.style.display = 'none';
  }

  _toggle(id) {
    if (this.mode === 'single') {
      if (this.selected.has(id)) {
        this.selected.clear();
      } else {
        this.selected.clear();
        this.selected.add(id);
        // Close after picking in single mode
        this._keepOpen = false;
        setTimeout(() => {
          this._open = false;
          this._list.style.display = 'none';
          this._input.value = '';
          this._q = '';
        }, 10);
      }
    } else {
      if (this.selected.has(id)) this.selected.delete(id);
      else this.selected.add(id);
    }
    this._renderChips();
    if (this._open) this._renderList();
    if (this.onChange) this.onChange(this.getSelected());
  }

  _remove(id) {
    this.selected.delete(id);
    this._renderChips();
    if (this._open) this._renderList();
    if (this.onChange) this.onChange(this.getSelected());
  }

  _renderChips() {
    this._chips.innerHTML = '';
    const sel = this.allLabels.filter(l => this.selected.has(l.id));
    if (sel.length === 0) {
      this._chips.style.display = 'none';
      return;
    }
    this._chips.style.display = '';
    sel.forEach(lbl => {
      const chip = document.createElement('span');
      chip.className = 'lp-chip';
      chip.appendChild(document.createTextNode(lbl.name));
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'lp-remove';
      rm.textContent = '×';
      rm.addEventListener('mousedown', e => { e.preventDefault(); this._keepOpen = false; this._remove(lbl.id); });
      rm.addEventListener('touchstart', e => { e.preventDefault(); this._remove(lbl.id); }, { passive: false });
      chip.appendChild(rm);
      this._chips.appendChild(chip);
    });
  }

  _renderList() {
    const q = this._q.toLowerCase().trim();
    const filtered = this.allLabels.filter(l => !q || l.name.toLowerCase().includes(q));
    this._list.innerHTML = '';

    filtered.forEach(lbl => {
      const opt = document.createElement('div');
      opt.className = 'lp-option' + (this.selected.has(lbl.id) ? ' lp-sel' : '');
      const ck = document.createElement('span');
      ck.className = 'lp-check';
      ck.textContent = this.selected.has(lbl.id) ? '✓' : '';
      opt.appendChild(ck);
      opt.appendChild(document.createTextNode(lbl.name));
      const bindToggle = e => { e.preventDefault(); this._keepOpen = true; this._toggle(lbl.id); };
      opt.addEventListener('mousedown', bindToggle);
      opt.addEventListener('touchstart', bindToggle, { passive: false });
      this._list.appendChild(opt);
    });

    if (this.allowCreate && this.mode === 'multi' && q &&
        !this.allLabels.some(l => l.name.toLowerCase() === q)) {
      const createOpt = document.createElement('div');
      createOpt.className = 'lp-create';
      createOpt.textContent = `Create "${this._q.trim()}"`;
      const bindCreate = e => { e.preventDefault(); this._keepOpen = true; this._doCreate(this._q.trim()); };
      createOpt.addEventListener('mousedown', bindCreate);
      createOpt.addEventListener('touchstart', bindCreate, { passive: false });
      this._list.appendChild(createOpt);
    }

    if (this._list.children.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'lp-empty';
      empty.textContent = q ? 'No matching labels.' :
        (this.allowCreate ? 'No labels yet — type to create one.' : 'No labels yet.');
      this._list.appendChild(empty);
    }
  }

  async _doCreate(name) {
    if (!name) return;
    try {
      const res = await fetch('/api/labels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!res.ok) throw new Error(await res.text());
      const lbl = await res.json();
      const { labels } = await fetch('/api/labels').then(r => r.json());
      this.setLabels(labels);
      this.selected.add(lbl.id);
      this._renderChips();
      if (this._open) this._renderList();
      this._input.value = '';
      this._q = '';
      if (this.onChange) this.onChange(this.getSelected());
      if (this.onLabelsRefreshed) this.onLabelsRefreshed(labels);
    } catch {
      // caller handles errors via onLabelsRefreshed absence or toast
    }
  }
}
