// Document Upload Page JavaScript
(function() {
    'use strict';

    // State
    let token = '';
    let selectedFiles = [];

    // Get token from URL path: /document-upload/{token}/
    function getToken() {
        var pathParts = window.location.pathname.split('/');
        var idx = pathParts.indexOf('document-upload') + 1;
        return pathParts[idx] || '';
    }

    // Show/hide steps
    function showStep(stepId) {
        var steps = ['step-loading', 'step-upload', 'step-uploading', 'step-success', 'step-error'];
        steps.forEach(function(id) {
            var el = document.getElementById(id);
            if (el) {
                if (id === stepId) {
                    el.classList.remove('hidden');
                    el.classList.add('fade-in');
                } else {
                    el.classList.add('hidden');
                }
            }
        });
    }

    function showError(message) {
        document.getElementById('error-message').textContent = message;
        showStep('step-error');
    }

    function formatFileSize(bytes) {
        if (!bytes) return '0 B';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    // Validate a single file client-side
    function validateFile(file) {
        var allowed = ['pdf', 'jpg', 'jpeg', 'png', 'bmp', 'tiff', 'webp'];
        var ext = file.name.split('.').pop().toLowerCase();
        if (allowed.indexOf(ext) === -1) {
            return 'File type not allowed. Use PDF, JPG, PNG, BMP, TIFF, or WebP.';
        }
        if (file.size > 10 * 1024 * 1024) {
            return 'File too large. Maximum size is 10MB.';
        }
        return null;
    }

    // Render selected files list
    function renderFileList() {
        var listEl = document.getElementById('file-list');
        var itemsEl = document.getElementById('file-list-items');
        var uploadBtn = document.getElementById('upload-button');

        if (selectedFiles.length === 0) {
            listEl.classList.add('hidden');
            uploadBtn.disabled = true;
            return;
        }

        listEl.classList.remove('hidden');
        uploadBtn.disabled = false;
        uploadBtn.textContent = 'Upload ' + selectedFiles.length + ' Document' + (selectedFiles.length > 1 ? 's' : '');

        itemsEl.innerHTML = '';
        selectedFiles.forEach(function(file, index) {
            var error = validateFile(file);
            var li = document.createElement('li');
            li.className = 'flex items-center justify-between p-2 bg-gray-50 rounded-lg';

            var infoDiv = document.createElement('div');
            infoDiv.className = 'flex-1 min-w-0';

            var nameP = document.createElement('p');
            nameP.className = 'text-sm font-medium text-gray-800 truncate';
            nameP.textContent = file.name;

            var sizeP = document.createElement('p');
            sizeP.className = 'text-xs ' + (error ? 'text-red-500' : 'text-gray-400');
            sizeP.textContent = error || formatFileSize(file.size);

            infoDiv.appendChild(nameP);
            infoDiv.appendChild(sizeP);

            var removeBtn = document.createElement('button');
            removeBtn.className = 'ml-2 p-1 text-gray-400 hover:text-red-500 transition-colors';
            removeBtn.innerHTML = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>';
            removeBtn.setAttribute('data-index', index);
            removeBtn.addEventListener('click', function() {
                selectedFiles.splice(parseInt(this.getAttribute('data-index')), 1);
                renderFileList();
            });

            li.appendChild(infoDiv);
            li.appendChild(removeBtn);
            itemsEl.appendChild(li);
        });
    }

    var MAX_FILES = 5;

    // Add files (from input or drag-and-drop)
    function addFiles(fileList) {
        for (var i = 0; i < fileList.length; i++) {
            if (selectedFiles.length >= MAX_FILES) {
                alert('You can upload a maximum of ' + MAX_FILES + ' documents at a time.');
                break;
            }
            selectedFiles.push(fileList[i]);
        }
        renderFileList();
    }

    // Step 1: Verify token
    async function verifyToken() {
        try {
            showStep('step-loading');
            var response = await fetch('/api/document-upload/verify/' + token + '/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
            });
            var data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || 'Invalid upload link');
            }

            document.getElementById('upload-heading').textContent = 'Upload Documents';
            document.getElementById('upload-details').textContent =
                'Dr. requested documents for ' + data.patient_name + ' at ' + data.clinic_name + '.';
            showStep('step-upload');
        } catch (error) {
            showError(error.message || 'This upload link is invalid or has expired.');
        }
    }

    // Upload files
    async function uploadFiles() {
        // Filter out invalid files
        var validFiles = selectedFiles.filter(function(f) { return !validateFile(f); });
        if (validFiles.length === 0) {
            alert('No valid files to upload. Please check file types and sizes.');
            return;
        }

        showStep('step-uploading');

        var formData = new FormData();
        var category = document.getElementById('category-select').value;
        formData.append('category', category);
        validFiles.forEach(function(file) {
            formData.append('files', file);
        });

        try {
            var response = await fetch('/api/document-upload/' + token + '/', {
                method: 'POST',
                body: formData,
            });
            var data = await response.json();

            if (!response.ok && (!data.uploaded || data.uploaded.length === 0)) {
                throw new Error(data.error || 'Upload failed');
            }

            // Show success
            var filesDiv = document.getElementById('success-files');
            if (data.uploaded && data.uploaded.length > 0) {
                var html = '<div class="bg-green-50 border border-green-200 rounded-lg p-4">' +
                    '<p class="text-sm font-semibold text-green-800 mb-2">Uploaded successfully:</p>' +
                    '<ul class="space-y-1">';
                data.uploaded.forEach(function(doc) {
                    html += '<li class="text-sm text-green-700 flex items-center">' +
                        '<svg class="w-4 h-4 mr-2 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">' +
                        '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg>' +
                        doc.title + ' (' + (doc.file_extension || '').toUpperCase() + ')</li>';
                });
                html += '</ul></div>';
                filesDiv.innerHTML = html;
            }

            // Show errors if any
            var errorsDiv = document.getElementById('success-errors');
            if (data.errors && data.errors.length > 0) {
                errorsDiv.classList.remove('hidden');
                var errHtml = '<div class="bg-red-50 border border-red-200 rounded-lg p-4">' +
                    '<p class="text-sm font-semibold text-red-800 mb-2">Some files could not be uploaded:</p>' +
                    '<ul class="space-y-1">';
                data.errors.forEach(function(err) {
                    errHtml += '<li class="text-sm text-red-700">' + err.file + ': ' + err.error + '</li>';
                });
                errHtml += '</ul></div>';
                errorsDiv.innerHTML = errHtml;
            } else {
                errorsDiv.classList.add('hidden');
            }

            showStep('step-success');
        } catch (error) {
            showError(error.message || 'Failed to upload documents. Please try again.');
        }
    }

    // Reset to upload form for "Upload More"
    function resetToUpload() {
        selectedFiles = [];
        var fileInput = document.getElementById('file-input');
        if (fileInput) fileInput.value = '';
        renderFileList();
        document.getElementById('success-files').innerHTML = '';
        document.getElementById('success-errors').innerHTML = '';
        document.getElementById('success-errors').classList.add('hidden');
        showStep('step-upload');
    }

    // Initialize
    document.addEventListener('DOMContentLoaded', function() {
        token = getToken();

        if (!token) {
            showError('No upload token provided.');
            return;
        }

        verifyToken();

        // Drop zone
        var dropZone = document.getElementById('drop-zone');
        var fileInput = document.getElementById('file-input');

        dropZone.addEventListener('click', function() {
            fileInput.click();
        });

        fileInput.addEventListener('change', function() {
            if (this.files.length > 0) {
                addFiles(this.files);
                this.value = '';
            }
        });

        dropZone.addEventListener('dragover', function(e) {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.add('drag-over');
        });

        dropZone.addEventListener('dragleave', function(e) {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.remove('drag-over');
        });

        dropZone.addEventListener('drop', function(e) {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.remove('drag-over');
            if (e.dataTransfer.files.length > 0) {
                addFiles(e.dataTransfer.files);
            }
        });

        // Upload button
        document.getElementById('upload-button').addEventListener('click', function() {
            uploadFiles();
        });

        // Upload more button
        document.getElementById('upload-more-button').addEventListener('click', function() {
            resetToUpload();
        });
    });
})();
