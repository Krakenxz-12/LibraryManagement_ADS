// Modern Library Management System JavaScript

function openModal(id) {
  const modal = document.getElementById(id);
  if (modal) {
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
  }
}

function closeModal(id) {
  const modal = document.getElementById(id);
  if (modal) {
    modal.classList.remove('active');
    document.body.style.overflow = '';
  }
}

// Close modal on Escape key or backdrop click
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-overlay.active').forEach(m => m.classList.remove('active'));
    document.body.style.overflow = '';
  }
});

document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal-overlay')) {
    e.target.classList.remove('active');
    document.body.style.overflow = '';
  }
});

// Mark notification as read
function markNotifRead(id, element) {
  fetch(`/notifications/${id}/read/`, {
    headers: { 'X-Requested-With': 'XMLHttpRequest' }
  }).then(r => r.json()).then(data => {
    if (data.status === 'ok') {
      element.closest('.notif-card').style.opacity = '0.5';
    }
  });
}

// Demo quick-fill credentials helper on login page
function fillLogin(username, password) {
  const uInput = document.getElementById('login-username');
  const pInput = document.getElementById('login-password');
  if (uInput && pInput) {
    uInput.value = username;
    pInput.value = password;
  }
}
