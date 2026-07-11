(function() {
    var key = localStorage.getItem('oraculo_api_key');
    if (!key) return; // sem chave de cliente = admin, acesso total (comportamento de sempre)

    var page = location.pathname.split('/').pop() || 'index.html';
    if (page === 'index.html' || page === '') return; // portal sempre abre; os cards é que filtram
    // admin.html pode ser restringida como qualquer outra página — se isso
    // te trancar pra fora, é só limpar a chave salva neste navegador
    // (botão "Sair" no index.html, ou localStorage.removeItem('oraculo_api_key')
    // no console) pra voltar a ser tratado como admin, acesso total.

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
