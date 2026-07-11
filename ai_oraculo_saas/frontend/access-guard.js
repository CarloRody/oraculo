(function() {
    var key = localStorage.getItem('oraculo_api_key');
    if (!key) return; // sem chave de cliente = admin, acesso total (comportamento de sempre)

    var page = location.pathname.split('/').pop() || 'index.html';
    if (page === 'index.html' || page === '') return; // portal sempre abre; os cards é que filtram
    if (page === 'admin.html') return; // painel admin nunca é bloqueado — é o único lugar pra corrigir a lista de acesso; bloqueá-lo também poderia trancar o próprio admin pra fora sem saída

    fetch(location.protocol + '//' + location.host + '/api/allowed-pages')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var allowed = data.pages || [];
            if (allowed.indexOf(page) === -1) {
                alert('Você não tem acesso a esta página.');
                location.href = 'index.html';
            }
        })
        .catch(function() { /* falha ao checar não bloqueia — mesma filosofia de falha aberta do resto do sistema */ });
})();
