// ── Toast notifications ────────────────────────────────────────
const toastIcons = {
  success: 'fa-circle-check',
  error:   'fa-circle-xmark',
  info:    'fa-circle-info'
};

function showToast(message, type = 'info', duration = 3500) {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    document.body.appendChild(container);
  }

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `<i class="fa-solid ${toastIcons[type] || toastIcons.info}"></i><span>${message}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.animation = 'fadeOut 0.3s ease forwards';
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

// ── Pest card hover accent glow ────────────────────────────────
document.querySelectorAll('.pest-card').forEach(card => {
  const color = card.dataset.color || '#3fb950';
  card.addEventListener('mouseenter', () => {
    card.style.boxShadow = `0 20px 48px rgba(0,0,0,0.5), 0 0 0 1px ${color}44`;
    card.querySelector('.pest-card-icon') &&
      (card.querySelector('.pest-card-icon').style.transform = 'scale(1.05) rotate(-2deg)');
  });
  card.addEventListener('mouseleave', () => {
    card.style.boxShadow = '';
    card.querySelector('.pest-card-icon') &&
      (card.querySelector('.pest-card-icon').style.transform = '');
  });
});

// ── Smooth page entrance animation ────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const targets = document.querySelectorAll(
    '.pest-card, .module-card, .census-panel, .result-panel, .coming-soon-card, .page-header'
  );
  targets.forEach((el, i) => {
    el.style.opacity = '0';
    el.style.transform = 'translateY(16px)';
    el.style.transition = 'opacity 0.45s ease, transform 0.45s ease';
    setTimeout(() => {
      el.style.opacity = '1';
      el.style.transform = 'translateY(0)';
    }, 80 + i * 70);
  });
});
