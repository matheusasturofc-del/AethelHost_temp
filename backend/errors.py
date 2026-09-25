"""Erro previsto (com código HTTP) que o servidor mostra como mensagem na tela."""


class ContentError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message
