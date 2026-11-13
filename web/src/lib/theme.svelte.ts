export type ThemeChoice = 'auto' | 'light' | 'dark';
const KEY = 'ictl-theme';

function read(): ThemeChoice {
  try {
    const saved = localStorage.getItem(KEY);
    return saved === 'light' || saved === 'dark' ? saved : 'auto';
  } catch {
    return 'auto';
  }
}

class Theme {
  choice = $state<ThemeChoice>(read());
  private media = typeof matchMedia === 'undefined' ? null : matchMedia('(prefers-color-scheme: dark)');
  dark = $state(false);

  constructor() {
    this.apply();
    this.media?.addEventListener('change', () => this.apply());
  }

  set(choice: ThemeChoice) {
    this.choice = choice;
    try {
      if (choice === 'auto') localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, choice);
    } catch {
      /* private mode: the choice lasts for this page only */
    }
    this.apply();
  }

  private apply() {
    this.dark = this.choice === 'dark' || (this.choice === 'auto' && !!this.media?.matches);
    if (typeof document !== 'undefined') document.documentElement.dataset.theme = this.dark ? 'dark' : 'light';
  }
}

export const theme = new Theme();
