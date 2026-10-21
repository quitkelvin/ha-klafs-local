// PIN keypad card for Home Assistant: starts a code lock (e.g. the sauna "Start (PIN)") in one step.
// type: custom:bubble-pin-card, entity: lock.… – after the last digit lock.unlock is called with the code;
// the code only travels in the service call (no helper, no history). Options: stellen (digits), titel, hinweis, gesendet, knopf.
class BubblePinCard extends HTMLElement {
  setConfig(c) {
    if (!c.entity) throw new Error('entity missing');
    this._c = { stellen: 4, titel: 'Enter PIN', knopf: 'Start', ...c };
    this._pin = '';
  }
  getCardSize() { return 5; }
  set hass(h) { this._h = h; if (!this.shadowRoot) this._bauen(); this._status(); }
  _bauen() {
    this.attachShadow({ mode: 'open' });
    const tasten = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '⌫', '0', '✓'];
    this.shadowRoot.innerHTML = `<style>
      .box { padding: 12px; background: var(--bubble-main-background-color, #1c1c1e); border-radius: 20px; text-align: center; }
      .t { font-size: 14px; font-weight: 600; color: var(--primary-text-color); }
      .s { font-size: 12px; color: var(--secondary-text-color); margin: 2px 0 8px; min-height: 16px; }
      .p { display: flex; justify-content: center; gap: 12px; margin: 6px 0 12px; }
      .p i { width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--secondary-text-color); }
      .p i.v { background: var(--primary-color, #0a84ff); border-color: var(--primary-color, #0a84ff); }
      .g { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; max-width: 280px; margin: 0 auto; }
      button { font: inherit; font-size: 20px; padding: 12px 0; border: 0; border-radius: 14px; cursor: pointer;
               background: rgba(255,255,255,.07); color: var(--primary-text-color); }
      button:active { background: rgba(255,255,255,.18); }
      button.ok { background: var(--primary-color, #0a84ff); color: #fff; font-size: 15px; }
    </style><ha-card style="background:none;border:none;box-shadow:none"><div class="box">
      <div class="t">${this._c.titel}</div><div class="s"></div>
      <div class="p">${Array.from({ length: this._c.stellen }, () => '<i></i>').join('')}</div>
      <div class="g">${tasten.map(k => `<button data-k="${k}" class="${k === '✓' ? 'ok' : ''}">${k === '✓' ? this._c.knopf : k}</button>`).join('')}</div>
    </div></ha-card>`;
    this.shadowRoot.querySelectorAll('button').forEach(b => b.addEventListener('click', () => this._taste(b.dataset.k)));
  }
  _punkte() { this.shadowRoot.querySelectorAll('.p i').forEach((p, i) => p.classList.toggle('v', i < this._pin.length)); }
  _status(text) {
    const st = this._h?.states?.[this._c.entity];
    const s = this.shadowRoot.querySelector('.s');
    if (text !== undefined) { this._meldung = text; this._bis = Date.now() + 6000; }
    if (this._meldung && Date.now() < this._bis) s.textContent = this._meldung;
    else s.textContent = this._c.hinweis || (st ? '' : 'entity not found');
  }
  async _taste(k) {
    if (k === '⌫') this._pin = this._pin.slice(0, -1);
    else if (k === '✓') { if (this._pin.length) await this._senden(); return; }
    else if (this._pin.length < this._c.stellen) this._pin += k;
    this._punkte();
    if (this._pin.length === this._c.stellen) await this._senden();
  }
  async _senden() {
    const code = this._pin; this._pin = ''; this._punkte();
    try {
      await this._h.callService('lock', 'unlock', { entity_id: this._c.entity, code });
      this._status(this._c.gesendet || 'Sent');
    } catch (e) { this._status('Error: ' + (e.message || e)); }
  }
}
customElements.define('bubble-pin-card', BubblePinCard);
window.customCards = window.customCards || []; window.customCards.push({ type: 'bubble-pin-card', name: 'PIN keypad (code lock)' });
