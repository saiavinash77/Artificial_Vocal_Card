import PyPDF2
import os
import sys

def extract_text_from_pdf(pdf_path):
    """Extract text from a PDF file"""
    text = ""
    try:
        with open(pdf_path, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            for page in reader.pages:
                text += page.extract_text() + "\n"
        return text
    except Exception as e:
        return f"Error extracting text: {str(e)}"

# Extract text from each PDF
pdf_files = [
    "AVC_PRD_Product_Requirements_Document.pdf",
    "AVC_TRD_Technical_Requirements_Document.pdf", 
    "Sathvani_AVC_Architecture_TechStack.pdf",
    "AVC_Implementation_Plan.pdf"
]

for pdf_file in pdf_files:
    if os.path.exists(pdf_file):
        print(f"\n{'='*80}")
        print(f"Extracting text from: {pdf_file}")
        print(f"{'='*80}\n")
        text = extract_text_from_pdf(pdf_file)
        
        # Save to text file
        text_file = pdf_file.replace('.pdf', '.txt')
        with open(text_file, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f"Text saved to: {text_file}")
        
        # Print first 500 characters to avoid encoding issues
        print(text[:500])
        print("... (truncated for display)")
    else:
        print(f"File not found: {pdf_file}")